"""
File Manager API

Provides web-based file browsing and editing for /config directory.
"""
import logging
import os
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, validator
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.audit import log_audit
from api.time_utils import to_utc_iso8601

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/files", tags=["File Manager"])

# Security: Only allow access to /config directory
CONFIG_ROOT = Path("/config")
ALLOWED_EXTENSIONS = {
    '.py', '.yaml', '.yml', '.json', '.txt', '.md', '.conf', 
    '.ini', '.env', '.log', '.sh', '.toml', '.example'
}
# Read-only extensions (can view but not edit)
READ_ONLY_EXTENSIONS = {'.example'}


class FileInfo(BaseModel):
    name: str
    path: str
    type: str  # 'file' or 'directory'
    size: int
    modified: str
    extension: str | None


class FileContent(BaseModel):
    path: str
    content: str


class FileOperation(BaseModel):
    path: str
    new_path: Optional[str] = None
    content: Optional[str] = None
    
    @validator('path', 'new_path')
    def validate_path(cls, v):
        if v and '..' in v:
            raise ValueError('Path traversal not allowed')
        return v


def validate_path(path: str) -> Path:
    """Validate and resolve path, ensuring it's within /config"""
    try:
        # Remove leading slash if present
        path = path.lstrip('/')
        
        # Resolve full path
        full_path = (CONFIG_ROOT / path).resolve()
        
        # Ensure path is within /config
        if not str(full_path).startswith(str(CONFIG_ROOT)):
            raise ValueError("Access denied: Path outside /config directory")
        
        return full_path
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {str(e)}")


@router.get("/browse")
async def browse_directory(path: str = ""):
    """List contents of a directory"""
    try:
        dir_path = validate_path(path)
        
        if not dir_path.exists():
            raise HTTPException(status_code=404, detail="Directory not found")
        
        if not dir_path.is_dir():
            raise HTTPException(status_code=400, detail="Path is not a directory")
        
        items: List[FileInfo] = []
        
        for item in sorted(dir_path.iterdir()):
            try:
                stat = item.stat()
                relative_path = str(item.relative_to(CONFIG_ROOT))
                
                items.append(FileInfo(
                    name=item.name,
                    path=f"/{relative_path}",
                    type="directory" if item.is_dir() else "file",
                    size=stat.st_size if item.is_file() else 0,
                    modified=to_utc_iso8601(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)),
                    extension=item.suffix if item.is_file() else None
                ))
            except Exception as e:
                logger.warning(f"Error reading item {item}: {e}")
                continue
        
        return {
            "path": f"/{path}" if path else "/",
            "items": items
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error browsing directory: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/read")
async def read_file(path: str):
    """Read file contents"""
    try:
        file_path = validate_path(path)
        
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        
        if not file_path.is_file():
            raise HTTPException(status_code=400, detail="Path is not a file")
        
        # Check file extension
        if file_path.suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400, 
                detail=f"File type {file_path.suffix} not allowed for editing"
            )
        
        # Check file size (max 1MB)
        if file_path.stat().st_size > 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large (max 1MB)")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        return {
            "path": f"/{str(file_path.relative_to(CONFIG_ROOT))}",
            "content": content,
            "size": file_path.stat().st_size,
            "modified": to_utc_iso8601(datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc))
        }
        
    except HTTPException:
        raise
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not text (binary file)")
    except Exception as e:
        logger.error(f"Error reading file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/save")
async def save_file(operation: FileOperation, db: AsyncSession = Depends(get_db)):
    """Save file contents with backup"""
    try:
        file_path = validate_path(operation.path)
        
        if not operation.content:
            raise HTTPException(status_code=400, detail="Content required")
        
        # Check file extension
        if file_path.suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400, 
                detail=f"File type {file_path.suffix} not allowed for editing"
            )
        
        # Check if read-only
        if file_path.suffix in READ_ONLY_EXTENSIONS:
            raise HTTPException(
                status_code=403, 
                detail=f"Files with {file_path.suffix} extension are read-only (view only)"
            )
        
        # Create backup if file exists
        if file_path.exists():
            backup_path = file_path.with_suffix(file_path.suffix + '.bak')
            shutil.copy2(file_path, backup_path)
            logger.info(f"Created backup: {backup_path}")
        
        # Write new content
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(operation.content)
        
        # Audit log
        await log_audit(
            db=db,
            action="update" if file_path.exists() else "create",
            resource_type="file",
            resource_name=str(file_path.relative_to(CONFIG_ROOT)),
            changes={"size": {"after": len(operation.content)}}
        )
        
        logger.info(f"Saved file: {file_path}")
        
        return {
            "success": True,
            "message": "File saved successfully",
            "path": f"/{str(file_path.relative_to(CONFIG_ROOT))}"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/create")
async def create_file(operation: FileOperation, db: AsyncSession = Depends(get_db)):
    """Create new file or directory"""
    try:
        file_path = validate_path(operation.path)
        
        if file_path.exists():
            raise HTTPException(status_code=400, detail="File or directory already exists")
        
        # Determine if creating file or directory based on presence of extension
        if file_path.suffix:
            # Create file
            if file_path.suffix not in ALLOWED_EXTENSIONS:
                raise HTTPException(
                    status_code=400, 
                    detail=f"File type {file_path.suffix} not allowed"
                )
            
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(operation.content or "", encoding='utf-8')
            
            resource_type = "file"
        else:
            # Create directory
            file_path.mkdir(parents=True, exist_ok=True)
            resource_type = "directory"
        
        # Audit log
        await log_audit(
            db=db,
            action="create",
            resource_type=resource_type,
            resource_name=str(file_path.relative_to(CONFIG_ROOT))
        )
        
        logger.info(f"Created {resource_type}: {file_path}")
        
        return {
            "success": True,
            "message": f"{resource_type.title()} created successfully",
            "path": f"/{str(file_path.relative_to(CONFIG_ROOT))}"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/copy")
async def copy_file(operation: FileOperation, db: AsyncSession = Depends(get_db)):
    """Copy file or directory"""
    try:
        src_path = validate_path(operation.path)
        
        if not operation.new_path:
            raise HTTPException(status_code=400, detail="new_path required")
        
        dst_path = validate_path(operation.new_path)
        
        if not src_path.exists():
            raise HTTPException(status_code=404, detail="Source not found")
        
        if dst_path.exists():
            raise HTTPException(status_code=400, detail="Destination already exists")
        
        # Copy
        if src_path.is_file():
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
        else:
            shutil.copytree(src_path, dst_path)
        
        # Audit log
        await log_audit(
            db=db,
            action="copy",
            resource_type="file" if src_path.is_file() else "directory",
            resource_name=str(src_path.relative_to(CONFIG_ROOT)),
            changes={"copied_to": str(dst_path.relative_to(CONFIG_ROOT))}
        )
        
        logger.info(f"Copied {src_path} to {dst_path}")
        
        return {
            "success": True,
            "message": "Copied successfully",
            "path": f"/{str(dst_path.relative_to(CONFIG_ROOT))}"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error copying: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rename")
async def rename_file(operation: FileOperation, db: AsyncSession = Depends(get_db)):
    """Rename or move file/directory"""
    try:
        src_path = validate_path(operation.path)
        
        if not operation.new_path:
            raise HTTPException(status_code=400, detail="new_path required")
        
        dst_path = validate_path(operation.new_path)
        
        if not src_path.exists():
            raise HTTPException(status_code=404, detail="Source not found")
        
        if dst_path.exists():
            raise HTTPException(status_code=400, detail="Destination already exists")
        
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        src_path.rename(dst_path)
        
        # Audit log
        await log_audit(
            db=db,
            action="rename",
            resource_type="file" if dst_path.is_file() else "directory",
            resource_name=str(src_path.relative_to(CONFIG_ROOT)),
            changes={
                "from": str(src_path.relative_to(CONFIG_ROOT)),
                "to": str(dst_path.relative_to(CONFIG_ROOT))
            }
        )
        
        logger.info(f"Renamed {src_path} to {dst_path}")
        
        return {
            "success": True,
            "message": "Renamed successfully",
            "path": f"/{str(dst_path.relative_to(CONFIG_ROOT))}"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error renaming: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/delete")
async def delete_file(path: str, db: AsyncSession = Depends(get_db)):
    """Delete file or directory"""
    try:
        file_path = validate_path(path)
        
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        
        # Safety: Don't allow deleting critical files
        critical_files = ['config.yaml']
        if file_path.name in critical_files:
            raise HTTPException(
                status_code=403, 
                detail=f"Cannot delete critical file: {file_path.name}"
            )
        
        is_dir = file_path.is_dir()
        
        # Delete
        if is_dir:
            shutil.rmtree(file_path)
        else:
            file_path.unlink()
        
        # Audit log
        await log_audit(
            db=db,
            action="delete",
            resource_type="directory" if is_dir else "file",
            resource_name=str(file_path.relative_to(CONFIG_ROOT))
        )
        
        logger.info(f"Deleted: {file_path}")
        
        return {
            "success": True,
            "message": "Deleted successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download")
async def download_file(path: str):
    """Download file"""
    try:
        file_path = validate_path(path)
        
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        
        if not file_path.is_file():
            raise HTTPException(status_code=400, detail="Path is not a file")
        
        return FileResponse(
            path=str(file_path),
            filename=file_path.name,
            media_type='application/octet-stream'
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        raise HTTPException(status_code=500, detail=str(e))
