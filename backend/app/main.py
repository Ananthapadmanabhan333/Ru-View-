"""Main FastAPI application entry point for RuView Fall Detection Backend."""
import asyncio
from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.database import init_db, AsyncSessionLocal
from app.services.node_tracker import node_tracker
from app.services.fall_manager import fall_manager
from app.services.ws_broadcaster import ws_broadcaster
from app.services.udp_listener import UdpIngestionServer
from app.services.ruview_client import RuViewWebSocketClient

from app.routers import health, nodes, rooms, falls, sensing, websockets

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ruview.backend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown orchestration."""
    logger.info("Initializing database...")
    await init_db()

    # Pre-populate tracked nodes from DB
    async with AsyncSessionLocal() as session:
        await node_tracker.initialize_from_db(session)

    # Wire event callbacks to WebSocket broadcaster
    node_tracker.set_event_callback(ws_broadcaster.broadcast)
    fall_manager.set_event_callback(ws_broadcaster.broadcast)

    # Start node monitor heartbeat loop
    monitor_task = asyncio.create_task(node_tracker.start_monitor_loop(AsyncSessionLocal))

    # Initialize UDP Ingestion Server if enabled
    udp_server = None
    if settings.UDP_ENABLED:
        udp_server = UdpIngestionServer(AsyncSessionLocal)
        await udp_server.start()

    # Initialize upstream RuView WebSocket bridge client if configured
    ruview_client = None
    if settings.RUVIEW_WS_URL:
        ruview_client = RuViewWebSocketClient(AsyncSessionLocal)
        await ruview_client.start()

    logger.info(f"{settings.PROJECT_NAME} v{settings.VERSION} initialized successfully.")
    yield

    # Clean shutdown
    logger.info("Shutting down services...")
    if udp_server:
        udp_server.stop()
    if ruview_client:
        ruview_client.stop()

    node_tracker.stop_monitor_loop()
    monitor_task.cancel()
    logger.info("Backend shutdown complete.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=(
        "Production backend connecting ESP32-S3 CSI sensor nodes to fall-detection "
        "applications through RuView wire protocols."
    ),
    lifespan=lifespan,
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global API Key middleware if configured
@app.middleware("http")
async def api_key_auth_middleware(request: Request, call_next):
    # Exempt health, docs, and open endpoints
    exempt_paths = ["/docs", "/openapi.json", "/redoc", "/api/health"]
    if any(request.url.path.startswith(p) for p in exempt_paths):
        return await call_next(request)

    if settings.API_KEY:
        header_key = request.headers.get("X-API-Key")
        if header_key != settings.API_KEY:
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})

    return await call_next(request)


# Include Routers under API prefix
app.include_router(health.router, prefix=settings.API_PREFIX)
app.include_router(nodes.router, prefix=settings.API_PREFIX)
app.include_router(rooms.router, prefix=settings.API_PREFIX)
app.include_router(falls.router, prefix=settings.API_PREFIX)
app.include_router(sensing.router, prefix=settings.API_PREFIX)

# Include WebSocket router at root (/ws/events)
app.include_router(websockets.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=settings.DEBUG)
