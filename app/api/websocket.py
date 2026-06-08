"""
WebSocket endpoints for real-time updates
Uses PostgreSQL NOTIFY/LISTEN for push notifications
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Set, Optional
import asyncio
import json
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


class ConnectionManager:
    """Manages WebSocket connections and broadcasts"""
    
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.listener_task: Optional[asyncio.Task] = None
        self.broadcaster_task: Optional[asyncio.Task] = None
        self.notification_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
    
    async def connect(self, websocket: WebSocket):
        """Accept new WebSocket connection"""
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")
        
        # Start background tasks if this is the first active connection.
        if len(self.active_connections) == 1:
            self.listener_task = asyncio.create_task(self._listen_postgres_notifications())
            self.broadcaster_task = asyncio.create_task(self._broadcast_worker())
    
    async def disconnect(self, websocket: WebSocket):
        """Remove WebSocket connection"""
        self.active_connections.discard(websocket)
        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")
        
        # Stop background tasks if no more connections.
        if len(self.active_connections) == 0:
            await self._stop_background_tasks()

    async def _stop_background_tasks(self):
        """Cancel and await background tasks so cleanup/finally blocks run."""
        tasks = [task for task in (self.listener_task, self.broadcaster_task) if task]
        self.listener_task = None
        self.broadcaster_task = None

        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        while not self.notification_queue.empty():
            try:
                self.notification_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
    
    async def broadcast(self, message: dict):
        """Send message to all connected clients"""
        if not self.active_connections:
            return
        
        message_json = json.dumps(message)
        disconnected = set()
        
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message_json)
            except Exception as e:
                logger.error(f"Error sending to WebSocket: {e}")
                disconnected.add(connection)
        
        # Clean up disconnected clients
        for conn in disconnected:
            await self.disconnect(conn)

    async def _broadcast_worker(self):
        """Serialize NOTIFY fan-out through a bounded queue to avoid task pileups."""
        try:
            while self.active_connections:
                try:
                    message = await asyncio.wait_for(self.notification_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                await self.broadcast(message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"WebSocket broadcast worker error: {e}")
    
    async def _listen_postgres_notifications(self):
        """
        Listen to PostgreSQL NOTIFY events and broadcast to WebSocket clients.
        Only runs when using PostgreSQL.
        """
        from core.database import engine
        
        conn = None
        try:
            import asyncpg
            
            # Extract connection details from SQLAlchemy engine
            url = engine.url
            
            # Create asyncpg connection for LISTEN
            conn = await asyncpg.connect(
                host=url.host,
                port=url.port or 5432,
                user=url.username,
                password=url.password,
                database=url.database
            )
            
            logger.info("🔔 PostgreSQL LISTEN started for real-time notifications")
            
            # Set up listeners for different channels
            await conn.add_listener('telemetry_update', self._handle_telemetry_notification)
            await conn.add_listener('miner_update', self._handle_miner_notification)
            
            # Keep connection alive
            while self.active_connections:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except ImportError:
            logger.warning("asyncpg not installed - PostgreSQL NOTIFY/LISTEN unavailable")
        except Exception as e:
            logger.error(f"PostgreSQL LISTEN error: {e}")
        finally:
            if conn is not None:
                try:
                    await conn.remove_listener('telemetry_update', self._handle_telemetry_notification)
                except Exception:
                    pass
                try:
                    await conn.remove_listener('miner_update', self._handle_miner_notification)
                except Exception:
                    pass
                try:
                    await conn.close()
                except Exception:
                    pass
            logger.info("🔕 PostgreSQL LISTEN stopped")

    def _enqueue_notification(self, message: dict):
        """Queue a websocket notification without spawning a task per event."""
        try:
            self.notification_queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.warning("WebSocket notification queue full; dropping message")
    
    def _handle_telemetry_notification(self, connection, pid, channel, payload):
        """Handle telemetry_update notifications"""
        try:
            data = json.loads(payload)
            self._enqueue_notification({
                "type": "telemetry_update",
                "data": data
            })
        except Exception as e:
            logger.error(f"Error handling telemetry notification: {e}")
    
    def _handle_miner_notification(self, connection, pid, channel, payload):
        """Handle miner_update notifications"""
        try:
            data = json.loads(payload)
            self._enqueue_notification({
                "type": "miner_update",
                "data": data
            })
        except Exception as e:
            logger.error(f"Error handling miner notification: {e}")


# Global connection manager
manager = ConnectionManager()


@router.websocket("/ws/updates")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time dashboard updates.
    
    Receives push notifications from PostgreSQL NOTIFY triggers:
    - telemetry_update: New telemetry data inserted
    - miner_update: Miner state/mode changed
    
    Message format:
    {
        "type": "telemetry_update" | "miner_update",
        "data": {...}
    }
    """
    await manager.connect(websocket)
    
    try:
        # Keep connection alive and handle client messages
        while True:
            # Wait for client messages (ping/pong for keepalive)
            data = await websocket.receive_text()
            
            # Handle ping
            if data == "ping":
                await websocket.send_text("pong")
            
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        await manager.disconnect(websocket)
