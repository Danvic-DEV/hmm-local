"""
Platform Update Management API
Checks GitHub Container Registry for new image versions and manages updates
"""
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from pathlib import Path
import subprocess
import logging
import httpx
import asyncio
import json
import os
from typing import Optional, List, Dict, Any
from datetime import datetime

from core.database import get_db, AsyncSessionLocal
from core.audit import log_audit
from sqlalchemy import select

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/updates", tags=["Platform Updates"])

# GitHub Container Registry configuration
GHCR_OWNER = "renegadeuk"
GHCR_MAIN_IMAGE_NAME = "hmm-local"
GHCR_UPDATER_IMAGE_NAME = "hmm-local-updater"
GHCR_REPO = GHCR_MAIN_IMAGE_NAME
GHCR_IMAGE = f"ghcr.io/{GHCR_OWNER}/{GHCR_REPO}"
GITHUB_API_URL = f"https://api.github.com/repos/{GHCR_OWNER}/{GHCR_REPO}"

# Local paths
GIT_COMMIT_FILE = Path("/app/.git_commit")
CONTAINER_NAME = "hmm-local"  # Default, will be detected

# Updater service URL (from environment)
UPDATER_URL = os.getenv("UPDATER_URL", "http://updater:8081")


async def get_github_cache(db: AsyncSession) -> Optional[dict]:
    """Get cached GitHub version data from database"""
    from core.database import PlatformVersionCache
    
    result = await db.execute(select(PlatformVersionCache).where(PlatformVersionCache.id == 1))
    cache = result.scalar_one_or_none()
    
    if not cache:
        return None
    
    return {
        "latest_commit": cache.latest_commit,
        "latest_commit_short": cache.latest_commit_short,
        "latest_message": cache.latest_message,
        "latest_author": cache.latest_author,
        "latest_date": cache.latest_date,
        "latest_tag": cache.latest_tag,
        "latest_image": cache.latest_image,
        "changelog": cache.changelog,
        "last_checked": cache.last_checked.isoformat(),
        "github_available": cache.github_available,
        "error_message": cache.error_message
    }


async def _fetch_ghcr_tags(image_name: str) -> Optional[set[str]]:
    """Fetch available tags for a GHCR image repository.

    Returns None if tags cannot be fetched.
    """
    scope = f"repository:{GHCR_OWNER}/{image_name}:pull"
    token_url = "https://ghcr.io/token"
    tags_url = f"https://ghcr.io/v2/{GHCR_OWNER}/{image_name}/tags/list"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            token_response = await client.get(
                token_url,
                params={"scope": scope, "service": "ghcr.io"}
            )
            token_response.raise_for_status()
            token_payload = token_response.json()
            token = token_payload.get("token") or token_payload.get("access_token")

            if not token:
                logger.warning("GHCR token response missing token for %s", image_name)
                return None

            tags_response = await client.get(
                tags_url,
                headers={"Authorization": f"Bearer {token}"}
            )
            tags_response.raise_for_status()
            tags_payload = tags_response.json()
            tags = tags_payload.get("tags") or []

            if not isinstance(tags, list):
                return None

            return {str(tag) for tag in tags}
    except Exception as e:
        logger.warning("Failed to fetch GHCR tags for %s: %s", image_name, e)
        return None


def _select_component_latest_tag(
    available_tags: set[str],
    preferred_tag: Optional[str],
    current_tag: Optional[str]
) -> str:
    """Pick a component tag with strong bias to published commit-tag preference.

    Priority:
    1) Preferred tag (cache-derived) if published
    2) Current tag if published (prevents false positives)
    3) Highest lexicographic main-* tag
    4) main / latest / highest lexicographic fallback
    """
    if preferred_tag and preferred_tag in available_tags:
        return preferred_tag

    if current_tag and current_tag in available_tags:
        return current_tag

    main_commit_tags = sorted(tag for tag in available_tags if tag.startswith("main-"))
    if main_commit_tags:
        return main_commit_tags[-1]

    if "main" in available_tags:
        return "main"

    if "latest" in available_tags:
        return "latest"

    return sorted(available_tags)[-1]


async def _resolve_latest_component_image(
    image_name: str,
    preferred_tag: Optional[str],
    current_tag: Optional[str],
    current_image: Optional[str] = None
) -> tuple[str, str, bool]:
    """Resolve latest published image tag for a specific component image.

    Returns: (latest_tag, latest_image, used_registry_tags)
    """
    available_tags = await _fetch_ghcr_tags(image_name)

    if available_tags:
        selected_tag = _select_component_latest_tag(
            available_tags=available_tags,
            preferred_tag=preferred_tag,
            current_tag=current_tag
        )
        selected_image = f"ghcr.io/{GHCR_OWNER}/{image_name}:{selected_tag}"
        if preferred_tag and preferred_tag != selected_tag:
            logger.info(
                "Preferred tag %s not published for %s, selected %s",
                preferred_tag,
                image_name,
                selected_tag
            )
        return selected_tag, selected_image, True

    fallback_tag = preferred_tag or current_tag or "unknown"
    fallback_image = (
        f"ghcr.io/{GHCR_OWNER}/{image_name}:{fallback_tag}"
        if fallback_tag != "unknown"
        else (current_image or f"ghcr.io/{GHCR_OWNER}/{image_name}:unknown")
    )
    return fallback_tag, fallback_image, False


def _normalize_changelog(raw_changelog: Any) -> List[Dict[str, Any]]:
    """Normalize cached changelog payload to a list of commit dictionaries."""
    if isinstance(raw_changelog, list):
        return [entry for entry in raw_changelog if isinstance(entry, dict)]

    if isinstance(raw_changelog, dict):
        commits = raw_changelog.get("commits", [])
        if isinstance(commits, list):
            return [entry for entry in commits if isinstance(entry, dict)]

    return []


class VersionInfo(BaseModel):
    current_image: str
    current_tag: str
    current_commit: str
    current_message: Optional[str]
    current_date: Optional[str]
    latest_commit: str
    latest_tag: str
    latest_message: str
    latest_date: str
    latest_image: str
    update_available: bool
    commits_behind: int


class CommitInfo(BaseModel):
    sha: str
    sha_short: str
    message: str
    author: str
    date: str
    url: str


class ContainerInfo(BaseModel):
    id: str
    name: str
    image: str
    network_mode: str
    ip_address: Optional[str]
    volumes: Dict[str, str]
    environment: Dict[str, str]
    restart_policy: str


class UpdateStatus(BaseModel):
    status: str  # 'idle', 'checking', 'pulling', 'stopping', 'starting', 'success', 'error'
    message: str
    progress: int  # 0-100
    started_at: Optional[str]
    completed_at: Optional[str]
    error: Optional[str]


# Global update status tracker
update_status = UpdateStatus(
    status="idle",
    message="No update in progress",
    progress=0,
    started_at=None,
    completed_at=None,
    error=None
)


def get_current_container_name() -> str:
    """Detect current container name from hostname"""
    try:
        import socket
        hostname = socket.gethostname()
        # Docker uses container ID as hostname, but we want the name
        # Try to find it via docker inspect
        result = subprocess.run(
            ["docker", "ps", "--filter", f"id={hostname}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return CONTAINER_NAME
    except:
        return CONTAINER_NAME


def get_container_info(container_name: Optional[str] = None) -> Optional[ContainerInfo]:
    """Get current container configuration via docker inspect
    
    Returns None if Docker socket is not available (production environment)
    """
    try:
        if not container_name:
            container_name = os.getenv("CONTAINER_NAME", "hmm-local")
        
        result = subprocess.run(
            ["docker", "inspect", container_name],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            # Docker socket not available - this is normal in production
            logger.info("Docker socket not available (production environment)")
            return None
        
        data = json.loads(result.stdout)[0]
        
        # Extract network settings
        network_mode = data["HostConfig"]["NetworkMode"]
        ip_address = None
        if "Networks" in data["NetworkSettings"]:
            for net_name, net_data in data["NetworkSettings"]["Networks"].items():
                if net_data.get("IPAddress"):
                    ip_address = net_data["IPAddress"]
                    break
        
        # Extract volumes
        volumes = {}
        if "Mounts" in data:
            for mount in data["Mounts"]:
                volumes[mount["Source"]] = mount["Destination"]
        
        # Extract environment variables
        env_dict = {}
        if "Config" in data and "Env" in data["Config"]:
            for env in data["Config"]["Env"]:
                if "=" in env:
                    key, value = env.split("=", 1)
                    env_dict[key] = value
        
        # Extract restart policy
        restart_policy = data["HostConfig"]["RestartPolicy"]["Name"]
        
        return ContainerInfo(
            id=data["Id"][:12],
            name=data["Name"].lstrip("/"),
            image=data["Config"]["Image"],
            network_mode=network_mode,
            ip_address=ip_address,
            volumes=volumes,
            environment=env_dict,
            restart_policy=restart_policy
        )
        
    except Exception as e:
        # Docker socket not available - this is expected in production
        logger.info(f"Cannot access Docker socket (production mode): {e}")
        return None


def get_current_commit() -> tuple[str, Optional[str], Optional[str]]:
    """Get current running commit hash from build"""
    try:
        if GIT_COMMIT_FILE.exists():
            commit_info = GIT_COMMIT_FILE.read_text().strip()
            # Format: "main-abc1234" or just "abc1234"
            if "-" in commit_info:
                branch, commit_hash = commit_info.split("-", 1)
            else:
                commit_hash = commit_info
            
            logger.info(f"Current commit from file: {commit_hash}")
            return commit_hash, None, None
        
        return "unknown", None, None
        
    except Exception as e:
        logger.error(f"Error getting current commit: {e}")
        return "unknown", None, None


async def get_github_commits(limit: int = 10) -> List[CommitInfo]:
    """Fetch recent commits from GitHub
    
    Returns empty list if rate limited or unavailable (graceful degradation)
    """
    try:
        url = f"{GITHUB_API_URL}/commits"
        params = {
            "sha": "main",
            "per_page": limit
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            
            # Handle rate limiting gracefully
            if response.status_code == 403:
                logger.warning("GitHub API rate limited - returning empty commits list")
                return []
            
            response.raise_for_status()
            
            commits = []
            data = response.json()
            
            for commit_data in data:
                commit = CommitInfo(
                    sha=commit_data["sha"],
                    sha_short=commit_data["sha"][:7],
                    message=commit_data["commit"]["message"].split("\n")[0],  # First line only
                    author=commit_data["commit"]["author"]["name"],
                    date=commit_data["commit"]["author"]["date"],
                    url=commit_data["html_url"]
                )
                commits.append(commit)
            
            return commits
            
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            logger.warning("GitHub API rate limited - returning empty commits list")
            return []
        logger.error(f"Error fetching GitHub commits: {e}")
        return []
    except Exception as e:
        logger.warning(f"Could not fetch GitHub commits: {e}")
        return []


@router.get("/container")
async def get_current_container() -> ContainerInfo:
    """Get current container configuration"""
    container = get_container_info()
    if container is None:
        # Return mock data for production environments without Docker socket
        container_name = os.getenv("CONTAINER_NAME", "hmm-local")
        return ContainerInfo(
            id="production",
            name=container_name,
            image=f"{GHCR_IMAGE}:production",
            network_mode="bridge",
            ip_address=None,
            volumes={"/config": "/config"},
            environment={},
            restart_policy="unless-stopped"
        )
    return container


@router.get("/check")
async def check_for_updates(db: AsyncSession = Depends(get_db)) -> VersionInfo:
    """Check if updates are available (uses database cache updated every 5 minutes)"""
    try:
        # Get current version from git commit file
        current_commit, current_message, current_date = get_current_commit()
        
        # Try to get container image info (may fail if Docker socket not available)
        current_image = None
        current_tag = None
        is_local_build = True  # Assume local build unless we can verify GHCR image
        
        container = get_container_info()
        if container:
            current_image = container.image
            is_local_build = not current_image.startswith("ghcr.io/")
            
            # Extract tag from image
            if ":" in current_image:
                current_tag = current_image.split(":")[-1]
            else:
                current_tag = "latest"
        else:
            logger.info("Docker socket not available, using environment info")
            # Production environment without Docker socket
            # Assume GHCR image based on commit
            pass
        
        # For local builds with "unknown" commit, try to get actual commit from git repo
        if current_commit == "unknown":
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd="/app"
                )
                if result.returncode == 0 and result.stdout.strip():
                    current_commit = result.stdout.strip()[:7]
                    logger.info(f"Using git commit from repo: {current_commit}")
            except Exception as e:
                logger.warning(f"Could not get git commit from repo: {e}")
        
        # If we couldn't get image info, construct it from commit
        if not current_image:
            if current_commit != "unknown":
                # Check git commit file to determine branch/tag format
                # Format in file: "main-abc1234" or "abc1234" (local dev)
                git_commit_file = Path("/app/.git_commit")
                tag_prefix = "dev"  # Default for local builds
                
                if git_commit_file.exists():
                    file_content = git_commit_file.read_text().strip()
                    # If file contains "main-", this is a production GHCR image
                    if file_content.startswith("main-"):
                        tag_prefix = "main"
                        is_local_build = False
                        logger.info(f"Detected production GHCR image from git commit file: {file_content}")
                
                current_tag = f"{tag_prefix}-{current_commit}"
                current_image = f"{GHCR_IMAGE}:{current_tag}"
            else:
                current_image = f"{GHCR_IMAGE}:unknown"
                current_tag = "unknown"
        elif is_local_build and current_tag == "latest" and current_commit != "unknown":
            # Enhance tag for local builds
            current_tag = f"dev-{current_commit}"
        
        # Get cached GitHub data from database
        github_cache = await get_github_cache(db)
        
        # If cache not available or GitHub unavailable, return current version only
        if not github_cache or not github_cache.get("github_available"):
            error_msg = github_cache.get("error_message") if github_cache else "GitHub cache not yet populated"
            logger.warning(f"Cannot fetch latest version from cache: {error_msg}")
            return VersionInfo(
                current_image=current_image or f"{GHCR_IMAGE}:unknown",
                current_tag=current_tag or "unknown",
                current_commit=current_commit,
                current_message=current_message,
                current_date=current_date,
                latest_commit=current_commit,  # Same as current since we can't check
                latest_tag=current_tag or "unknown",
                latest_message=f"GitHub unavailable: {error_msg}",
                latest_date=current_date or "",
                latest_image=current_image or f"{GHCR_IMAGE}:unknown",
                update_available=False,  # Can't determine, assume no update
                commits_behind=0
            )
        
        latest_commit = github_cache["latest_commit"]
        latest_commit_short = github_cache["latest_commit_short"]
        cached_latest_tag = github_cache["latest_tag"]
        latest_tag, latest_image, _ = await _resolve_latest_component_image(
            image_name=GHCR_MAIN_IMAGE_NAME,
            preferred_tag=cached_latest_tag,
            current_tag=current_tag,
            current_image=current_image,
        )
        latest_message = github_cache["latest_message"]
        latest_date = github_cache["latest_date"]
        
        # Check if update available (component-published tag comparison)
        update_available = (
            (current_tag or "unknown") != "unknown"
            and (latest_tag or "unknown") != "unknown"
            and (current_tag != latest_tag)
        )
        
        # Count commits behind (check if current commit is in changelog)
        commits_behind = 0
        if update_available and current_commit != "unknown":
            changelog = _normalize_changelog(github_cache.get("changelog", []))
            found_current = False
            for idx, commit in enumerate(changelog):
                if commit["sha"] == current_commit:
                    found_current = True
                    commits_behind = idx
                    break
            if not found_current:
                commits_behind = len(changelog)  # More than we have cached
        
        return VersionInfo(
            current_image=current_image,
            current_tag=current_tag,
            current_commit=current_commit,
            current_message=current_message,
            current_date=current_date,
            latest_commit=latest_commit,
            latest_tag=latest_tag,
            latest_message=latest_message,
            latest_date=latest_date,
            latest_image=latest_image,
            update_available=update_available,
            commits_behind=commits_behind
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking for updates: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/changelog")
async def get_changelog(db: AsyncSession = Depends(get_db), limit: int = 20) -> List[CommitInfo]:
    """Get recent commits/changelog (uses database cache)
    
    Returns empty list if GitHub is unavailable (graceful degradation)
    """
    github_cache = await get_github_cache(db)
    if not github_cache or not github_cache.get("github_available"):
        return []  # Return empty list if cache unavailable
    
    changelog = _normalize_changelog(github_cache.get("changelog", []))
    # Convert to CommitInfo objects
    result = []
    for commit in changelog[:limit]:
        result.append(CommitInfo(
            sha=commit["sha"],
            sha_short=commit.get("sha_short", commit["sha"][:7]),  # Fallback for old cache
            message=commit["message"],
            author=commit["author"],
            date=commit["date"],
            url=commit.get("url", f"https://github.com/RenegadeUK/hmm-local/commit/{commit['sha']}")
        ))
    return result


@router.post("/refresh")
async def refresh_github_cache():
    """Manually trigger GitHub cache refresh (bypasses 5-minute schedule)"""
    from core.scheduler import scheduler
    
    if not scheduler:
        raise HTTPException(status_code=503, detail="Scheduler service not available")
    
    # Trigger immediate cache update
    try:
        await scheduler._update_platform_version_cache()
        return {"success": True, "message": "Cache refresh triggered"}
    except Exception as e:
        logger.error(f"Manual cache refresh failed: {e}")
        raise HTTPException(status_code=500, detail=f"Cache refresh failed: {str(e)}")


@router.get("/status")
async def get_update_status() -> UpdateStatus:
    """Get current update operation status"""
    return update_status


@router.get("/updater-health")
async def get_updater_health():
    """Check if updater service is available"""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{UPDATER_URL}/health")
            response.raise_for_status()
            data = response.json()
            return data
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=503,
            detail=f"Updater service timeout. Ensure updater container is running at {UPDATER_URL}"
        )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot connect to updater service at {UPDATER_URL}. Deploy updater container first."
        )
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Updater service unavailable: {str(e)}"
        )


@router.get("/updater-version")
async def get_updater_version(db: AsyncSession = Depends(get_db)):
    """Get current updater container version and check for updates"""
    try:
        # Get updater container info via Docker
        result = subprocess.run(
            ["docker", "inspect", "hmm-local-updater"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode != 0:
            return {
                "available": False,
                "error": "Updater container not found"
            }
        
        container_data = json.loads(result.stdout)[0]
        current_image = container_data["Config"]["Image"]
        
        # Extract current tag
        current_tag = current_image.split(":")[-1] if ":" in current_image else "main"
        
        # Get latest published updater tag (cache preferred, GHCR-validated)
        github_cache = await get_github_cache(db)
        preferred_tag = github_cache["latest_tag"] if github_cache and github_cache.get("github_available") else None
        latest_tag, latest_image, _ = await _resolve_latest_component_image(
            image_name=GHCR_UPDATER_IMAGE_NAME,
            preferred_tag=preferred_tag,
            current_tag=current_tag,
            current_image=current_image,
        )
        
        # Check if update is needed
        update_available = (
            (current_tag or "unknown") != "unknown"
            and (latest_tag or "unknown") != "unknown"
            and (current_tag != latest_tag)
        )
        
        return {
            "available": True,
            "current_image": current_image,
            "current_tag": current_tag,
            "latest_image": latest_image,
            "latest_tag": latest_tag,
            "update_available": update_available
        }
        
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "error": "Docker command timed out"
        }
    except Exception as e:
        logger.error(f"Failed to get updater version: {e}")
        return {
            "available": False,
            "error": str(e)
        }


@router.post("/updater/update")
async def update_updater(db: AsyncSession = Depends(get_db)):
    """Update the updater sidecar container itself"""
    try:
        # Resolve latest published updater image tag
        github_cache = await get_github_cache(db)
        preferred_tag = github_cache["latest_tag"] if github_cache and github_cache.get("github_available") else None
        latest_tag, latest_image, _ = await _resolve_latest_component_image(
            image_name=GHCR_UPDATER_IMAGE_NAME,
            preferred_tag=preferred_tag,
            current_tag=None,
        )
        
        # Step 1: Inspect current container to preserve configuration
        logger.info("Inspecting hmm-local-updater container configuration...")
        inspect_result = subprocess.run(
            ["docker", "inspect", "hmm-local-updater"],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if inspect_result.returncode != 0:
            raise Exception(f"Failed to inspect container: {inspect_result.stderr}")
        
        import json
        container_info = json.loads(inspect_result.stdout)[0]
        
        # Extract configuration
        config = container_info["Config"]
        host_config = container_info["HostConfig"]
        network_settings = container_info["NetworkSettings"]
        
        # Preserve environment variables
        env_vars = [e for e in config.get("Env", []) if e.startswith("AUTO_DEPLOY_ENABLED=")]
        
        # Preserve volumes
        binds = host_config.get("Binds", [])
        
        # Preserve network configuration
        network_mode = host_config.get("NetworkMode", "bridge")
        networks = network_settings.get("Networks", {})
        static_ip = None
        if "br0" in networks:
            ipam_config = networks["br0"].get("IPAMConfig", {})
            static_ip = ipam_config.get("IPv4Address")
        
        # Preserve restart policy
        restart_policy = host_config.get("RestartPolicy", {}).get("Name", "unless-stopped")
        
        logger.info(f"Preserved config - Network: {network_mode}, IP: {static_ip}, Restart: {restart_policy}")
        
        # Audit log
        await log_audit(
            db=db,
            action="update",
            resource_type="updater",
            resource_name="hmm-local-updater",
            changes={
                "action": "recreate_with_config_preservation",
                "image": latest_image,
                "network": network_mode,
                "static_ip": static_ip
            }
        )
        
        # Step 2: Pull latest updater image BEFORE stopping container
        logger.info(f"Pulling hmm-local-updater image: {latest_image}...")
        pull_result = subprocess.run(
            ["docker", "pull", latest_image],
            capture_output=True,
            text=True,
            timeout=120
        )
        
        if pull_result.returncode != 0:
            raise Exception(f"Failed to pull image: {pull_result.stderr}")
        
        # Step 3: Stop container
        logger.info("Stopping updater container...")
        stop_result = subprocess.run(
            ["docker", "stop", "hmm-local-updater"],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if stop_result.returncode != 0:
            logger.warning(f"Stop returned non-zero: {stop_result.stderr}")
        
        # Step 4: Remove container
        logger.info("Removing updater container...")
        rm_result = subprocess.run(
            ["docker", "rm", "hmm-local-updater"],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if rm_result.returncode != 0:
            raise Exception(f"Failed to remove container: {rm_result.stderr}")
        
        # Step 5: Recreate container with preserved configuration
        logger.info("Recreating updater container with preserved configuration...")
        
        docker_cmd = [
            "docker", "run", "-d",
            "--name", "hmm-local-updater",
            "--network", network_mode,
            "--restart", restart_policy
        ]
        
        # Add static IP if present
        if static_ip:
            docker_cmd.extend(["--ip", static_ip])
        
        # Add environment variables
        for env in env_vars:
            docker_cmd.extend(["-e", env])
        
        # Add volume binds
        for bind in binds:
            docker_cmd.extend(["-v", bind])
        
        # Add image
        docker_cmd.append(latest_image)
        
        logger.info(f"Running: {' '.join(docker_cmd)}")
        
        create_result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if create_result.returncode != 0:
            raise Exception(f"Failed to create container: {create_result.stderr}")
        
        logger.info("✅ Updater container updated successfully")
        
        return {
            "success": True,
            "message": "Updater container updated and recreated with preserved configuration",
            "config_preserved": {
                "network": network_mode,
                "static_ip": static_ip,
                "restart_policy": restart_policy,
                "volumes": len(binds),
                "env_vars": len(env_vars)
            }
        }
        
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="Update operation timed out"
        )
    except Exception as e:
        logger.error(f"Failed to update updater: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Update failed: {str(e)}"
        )


@router.post("/apply")
async def apply_update(db: AsyncSession = Depends(get_db)):
    """
    Apply platform update by pulling latest image and recreating container
    
    NOTE: This will restart the container with the same configuration!
    Your /config data and network settings will be preserved.
    """
    global update_status
    
    if update_status.status not in ["idle", "success", "error"]:
        raise HTTPException(status_code=409, detail="Update already in progress")
    
    # Get version info to ensure update is available
    version_info = await check_for_updates(db)
    if not version_info.update_available:
        raise HTTPException(status_code=400, detail="Already on latest version")
    
    # Reset status
    update_status = UpdateStatus(
        status="checking",
        message="Preparing update...",
        progress=10,
        started_at=datetime.utcnow().isoformat(),
        completed_at=None,
        error=None
    )
    
    # Audit log
    await log_audit(
        db=db,
        action="update",
        resource_type="platform",
        resource_name="hmm-local",
        changes={
            "from_image": version_info.current_image,
            "to_image": version_info.latest_image,
            "commits_behind": version_info.commits_behind
        }
    )
    
    # Start update in background (don't pass db session - it will create its own)
    asyncio.create_task(run_update(version_info.latest_image))
    
    return {
        "success": True,
        "message": "Update started. Container will restart shortly.",
        "new_image": version_info.latest_image,
        "status": update_status
    }


async def run_update(new_image: str):
    """Background task to perform the actual update via updater service"""
    global update_status
    
    try:
        # Get current container name from environment or use default
        update_status.status = "checking"
        update_status.message = "Preparing update..."
        update_status.progress = 20
        logger.info("Preparing update request...")
        
        # Container name from CONTAINER_NAME environment variable, fallback to hostname
        container_name = os.environ.get("CONTAINER_NAME") or os.environ.get("HOSTNAME", "hmm-local")
        logger.info(f"Target container: {container_name}, New image: {new_image}")
        
        # Step 1: Verify updater service is available
        update_status.status = "connecting"
        update_status.message = "Connecting to updater service..."
        update_status.progress = 40
        
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                health_response = await client.get(f"{UPDATER_URL}/health")
                health_response.raise_for_status()
                logger.info(f"✅ Updater service is healthy")
        except Exception as e:
            raise Exception(f"Updater service unavailable: {e}. Ensure hmm-local-updater container is running.")
        
        # Step 2: Call updater service to perform the update
        update_status.status = "updating"
        update_status.message = "Requesting update from updater service... Container will restart."
        update_status.progress = 60
        logger.info(f"Calling updater service to update {container_name} to {new_image}")
        
        async with httpx.AsyncClient(timeout=120.0) as client:
            update_response = await client.post(
                f"{UPDATER_URL}/update",
                json={
                    "container_name": container_name,
                    "new_image": new_image
                }
            )
            
            if update_response.status_code == 200:
                result = update_response.json()
                logger.info(f"✅ Updater service accepted request: {result.get('message')}")
                
                # Success!
                update_status.status = "success"
                update_status.message = "Update initiated successfully! Container is restarting."
                update_status.progress = 100
                update_status.completed_at = datetime.utcnow().isoformat()
                
                # Audit log success (create new session)
                try:
                    async with AsyncSessionLocal() as db:
                        await log_audit(
                            db=db,
                            action="update",
                            resource_type="platform",
                            resource_name="hmm-local",
                            changes={
                                "status": "success",
                                "new_image": new_image,
                                "container_id": result.get('container_id')
                            }
                        )
                except Exception as audit_error:
                    logger.warning(f"Failed to log audit: {audit_error}")
                
                logger.info("Platform update completed successfully")
            else:
                error_data = update_response.json()
                raise Exception(f"Updater service failed: {error_data.get('error', 'Unknown error')}")
        
    except httpx.TimeoutException:
        # This is expected - the container is being stopped
        update_status.status = "restarting"
        update_status.message = "Update in progress, container restarting..."
        update_status.progress = 90
        logger.info("Connection lost (expected during update)")
        
    except Exception as e:
        update_status.status = "error"
        update_status.message = "Update failed"
        update_status.error = str(e)
        update_status.completed_at = datetime.utcnow().isoformat()
        logger.error(f"Update failed: {e}")
        
        # Audit log failure (create new session)
        try:
            async with AsyncSessionLocal() as db:
                await log_audit(
                    db=db,
                    action="update",
                    resource_type="platform",
                    resource_name="hmm-local",
                    changes={"status": "error", "error": str(e)}
                )
        except Exception as audit_error:
            logger.warning(f"Failed to log audit: {audit_error}")
        # Use nohup to detach from this process
        subprocess.Popen(
            ["nohup", "sh", "-c", f"sleep 1 && {script_path} &"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True  # Fully detach from parent process
        )
        
        # Success!
        update_status.status = "success"
        update_status.message = "Update completed successfully! New container is running."
        update_status.progress = 100
        update_status.completed_at = datetime.utcnow().isoformat()
        
        # Audit log success (create new session)
        try:
            async with AsyncSessionLocal() as db:
                await log_audit(
                    db=db,
                    action="update",
                    resource_type="platform",
                    resource_name="hmm-local",
                    changes={"status": "success", "new_container_id": new_container_id}
                )
        except Exception as audit_error:
            logger.warning(f"Failed to log audit: {audit_error}")
        
        logger.info("Platform update completed successfully")
        
    except subprocess.TimeoutExpired:
        update_status.status = "error"
        update_status.message = "Update timed out"
        update_status.error = "Operation took too long and was cancelled"
        update_status.completed_at = datetime.utcnow().isoformat()
        logger.error("Update timed out")
        
    except Exception as e:
        update_status.status = "error"
        update_status.message = "Update failed"
        update_status.error = str(e)
        update_status.completed_at = datetime.utcnow().isoformat()
        logger.error(f"Update failed: {e}")
        
        # Audit log failure (create new session)
        try:
            async with AsyncSessionLocal() as db:
                await log_audit(
                    db=db,
                    action="update",
                    resource_type="platform",
                    resource_name="hmm-local",
                    changes={"status": "error", "error": str(e)}
                )
        except Exception as audit_error:
            logger.warning(f"Failed to log audit: {audit_error}")
