from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import logging
import mysql.connector
from mysql.connector import Error
import uuid
from datetime import datetime
from typing import List, Optional
import shutil
from pathlib import Path
import mimetypes
import time

# Document processing imports
try:
    import PyPDF2
except ImportError:
    PyPDF2 = None

try:
    import docx
except ImportError:
    docx = None

import csv
import json

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(title="File Service", version="2.0.0")

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/app/uploads")
DB_HOST = os.getenv("DB_HOST", "mysql")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "bedrock_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "bedrock_password")
DB_NAME = os.getenv("DB_NAME", "bedrock_chat")
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTENSIONS = {'.pdf', '.txt', '.docx', '.csv', '.json', '.md'}

# Create upload directory
Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)

# Database connection with retry logic
def get_db_connection(max_retries=5):
    for attempt in range(max_retries):
        try:
            connection = mysql.connector.connect(
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USER,
                password=DB_PASSWORD,
                database=DB_NAME,
                connect_timeout=10,
                autocommit=True
            )
            if connection.is_connected():
                return connection
        except Error as e:
            logger.warning(f"Database connection attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                logger.error(f"Database connection failed after {max_retries} attempts")
                return None

# Response models
class UploadResponse(BaseModel):
    message: str
    files: List[dict]

class FileInfo(BaseModel):
    id: int
    filename: str
    original_name: str
    file_type: str
    file_size: int
    upload_date: str
    has_text: bool

@app.get("/")
async def root():
    return {
        "message": "File Service is running",
        "version": "2.0.0",
        "supported_formats": list(ALLOWED_EXTENSIONS),
        "max_file_size": f"{MAX_FILE_SIZE // (1024*1024)}MB"
    }

@app.get("/health")
async def health_check():
    """Comprehensive health check"""
    status = {"status": "healthy", "service": "file-service"}
    
    # Check database connectivity
    try:
        connection = get_db_connection(max_retries=1)
        if connection:
            connection.close()
            status["database"] = "healthy"
        else:
            status["database"] = "unhealthy"
            status["status"] = "degraded"
    except:
        status["database"] = "unhealthy"
        status["status"] = "degraded"
    
    # Check upload directory
    try:
        upload_path = Path(UPLOAD_DIR)
        if upload_path.exists() and upload_path.is_dir():
            status["storage"] = "healthy"
        else:
            status["storage"] = "unhealthy"
            status["status"] = "degraded"
    except:
        status["storage"] = "unhealthy"
        status["status"] = "degraded"
    
    return status

@app.post("/upload", response_model=UploadResponse)
async def upload_files(
    files: List[UploadFile] = File(...),
    session_id: str = Form(...)
):
    """Upload and process files with enhanced error handling"""
    uploaded_files = []
    
    try:
        for file in files:
            # Validate file size
            file_content = await file.read()
            if len(file_content) > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=400,
                    detail=f"File {file.filename} is too large. Max size is {MAX_FILE_SIZE // (1024*1024)}MB"
                )
            
            # Validate file extension
            file_ext = Path(file.filename).suffix.lower()
            if file_ext not in ALLOWED_EXTENSIONS:
                raise HTTPException(
                    status_code=400,
                    detail=f"File type {file_ext} not supported. Supported types: {list(ALLOWED_EXTENSIONS)}"
                )
            
            # Generate unique filename
            unique_filename = f"{uuid.uuid4()}{file_ext}"
            file_path = Path(UPLOAD_DIR) / unique_filename
            
            # Save file
            with open(file_path, "wb") as buffer:
                buffer.write(file_content)
            
            # Extract text from file
            extracted_text = extract_text_from_file(file_path, file_ext)
            
            # Store file info in database
            file_info = store_file_info(
                session_id=session_id,
                filename=unique_filename,
                original_name=file.filename,
                file_type=file_ext,
                file_size=len(file_content),
                file_path=str(file_path),
                extracted_text=extracted_text
            )
            
            uploaded_files.append(file_info)
            logger.info(f"Successfully uploaded and processed: {file.filename}")
        
        return UploadResponse(
            message=f"Successfully uploaded {len(uploaded_files)} files",
            files=uploaded_files
        )
        
    except Exception as e:
        logger.error(f"Upload error: {str(e)}")
        # Clean up any uploaded files on error
        for file_info in uploaded_files:
            try:
                file_path = file_info.get('file_path', '')
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
            except:
                pass
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/files/{session_id}")
async def get_files(session_id: str):
    """Get uploaded files for a session with their content"""
    try:
        connection = get_db_connection()
        if not connection:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        cursor = connection.cursor()
        cursor.execute("""
            SELECT id, filename, original_name, file_type, file_size, upload_date, extracted_text
            FROM uploaded_files 
            WHERE session_id = %s 
            ORDER BY upload_date DESC
        """, (session_id,))
        
        results = cursor.fetchall()
        cursor.close()
        connection.close()
        
        files = []
        for row in results:
            file_info = {
                "id": row[0],
                "filename": row[2],  # Use original_name for display
                "content": row[6] if row[6] else "No content available",  # extracted_text
                "content_type": row[3],  # file_type
                "file_size": row[4],
                "upload_date": row[5].isoformat() if hasattr(row[5], 'isoformat') else str(row[5]),
                "has_text": bool(row[6])
            }
            files.append(file_info)
        
        logger.info(f"Retrieved {len(files)} files for session {session_id}")
        return {"session_id": session_id, "files": files}
        
    except Exception as e:
        logger.error(f"Error retrieving files: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to retrieve files")

@app.get("/file/content/{file_id}")
async def get_file_content(file_id: int):
    """Get extracted text content from a file"""
    try:
        connection = get_db_connection()
        if not connection:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        cursor = connection.cursor()
        cursor.execute("""
            SELECT original_name, extracted_text 
            FROM uploaded_files 
            WHERE id = %s
        """, (file_id,))
        
        result = cursor.fetchone()
        cursor.close()
        connection.close()
        
        if not result:
            raise HTTPException(status_code=404, detail="File not found")
        
        return {
            "file_id": file_id,
            "filename": result[0],
            "content": result[1] or "No text content available"
        }
        
    except Exception as e:
        logger.error(f"Error retrieving file content: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to retrieve file content")

@app.delete("/file/{file_id}")
async def delete_file(file_id: int):
    """Delete a file"""
    try:
        connection = get_db_connection()
        if not connection:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        cursor = connection.cursor()
        
        # Get file path first
        cursor.execute("SELECT file_path FROM uploaded_files WHERE id = %s", (file_id,))
        result = cursor.fetchone()
        
        if not result:
            raise HTTPException(status_code=404, detail="File not found")
        
        file_path = result[0]
        
        # Delete from database
        cursor.execute("DELETE FROM uploaded_files WHERE id = %s", (file_id,))
        
        cursor.close()
        connection.close()
        
        # Delete physical file
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except FileNotFoundError:
            pass  # File already deleted
        
        return {"message": "File deleted successfully"}
        
    except Exception as e:
        logger.error(f"Error deleting file: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to delete file")

def extract_text_from_file(file_path: Path, file_ext: str) -> Optional[str]:
    """Extract text content from uploaded file with better error handling"""
    try:
        if file_ext == '.txt' or file_ext == '.md':
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                logger.info(f"Extracted {len(content)} characters from text file")
                return content
        
        elif file_ext == '.pdf' and PyPDF2:
            try:
                with open(file_path, 'rb') as f:
                    pdf_reader = PyPDF2.PdfReader(f)
                    text = ""
                    for page in pdf_reader.pages:
                        text += page.extract_text() + "\n"
                    logger.info(f"Extracted {len(text)} characters from PDF")
                    return text
            except Exception as e:
                logger.error(f"PDF extraction error: {e}")
                return "PDF content could not be extracted"
        
        elif file_ext == '.docx' and docx:
            try:
                doc = docx.Document(file_path)
                text = ""
                for paragraph in doc.paragraphs:
                    text += paragraph.text + "\n"
                logger.info(f"Extracted {len(text)} characters from DOCX")
                return text
            except Exception as e:
                logger.error(f"DOCX extraction error: {e}")
                return "DOCX content could not be extracted"
        
        elif file_ext == '.csv':
            try:
                text = ""
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    csv_reader = csv.reader(f)
                    for row in csv_reader:
                        text += ", ".join(row) + "\n"
                logger.info(f"Extracted {len(text)} characters from CSV")
                return text
            except Exception as e:
                logger.error(f"CSV extraction error: {e}")
                return "CSV content could not be extracted"
        
        elif file_ext == '.json':
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    data = json.load(f)
                    text = json.dumps(data, indent=2)
                    logger.info(f"Extracted {len(text)} characters from JSON")
                    return text
            except Exception as e:
                logger.error(f"JSON extraction error: {e}")
                return "JSON content could not be extracted"
        
        else:
            logger.warning(f"Unsupported file extension: {file_ext}")
            return "File type not supported for text extraction"
            
    except Exception as e:
        logger.error(f"Error extracting text from {file_path}: {str(e)}")
        return "Text extraction failed"

def store_file_info(session_id: str, filename: str, original_name: str, 
                   file_type: str, file_size: int, file_path: str, 
                   extracted_text: Optional[str]) -> dict:
    """Store file information in database with better error handling"""
    try:
        connection = get_db_connection()
        if not connection:
            raise Exception("Database connection failed")
        
        cursor = connection.cursor()
        cursor.execute("""
            INSERT INTO uploaded_files 
            (session_id, filename, original_name, file_type, file_size, file_path, extracted_text) 
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (session_id, filename, original_name, file_type, file_size, file_path, extracted_text))
        
        file_id = cursor.lastrowid
        cursor.close()
        connection.close()
        
        logger.info(f"Stored file info for {original_name} (ID: {file_id})")
        
        return {
            "id": file_id,
            "filename": filename,
            "original_name": original_name,
            "file_type": file_type,
            "file_size": file_size,
            "has_text": bool(extracted_text),
            "file_path": file_path
        }
        
    except Exception as e:
        logger.error(f"Error storing file info: {str(e)}")
        raise Exception(f"Failed to store file info: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7000)
