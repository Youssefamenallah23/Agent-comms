"""FastAPI application: agents, background loops and the live dashboard."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import runtime
from .config import settings
from .db import repo
from .services import pipeline
from .simulation import seed
from .api.routes import router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


async def _loop(interval: int, coro_factory, name: str):
    logger = logging.getLogger(name)
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("loop error: %s", exc)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    repo.init_db()
    if settings.seed_on_start and repo.is_empty():
        seed.seed_data()
        logging.getLogger("campus").info("Seeded demo campus data")
    runtime.setup()
    tasks = []
    if settings.background_tasks:
        if settings.auto_resolve:
            tasks.append(asyncio.create_task(_loop(
                settings.scan_interval_sec, pipeline.scan_and_resolve, "pipeline")))
        tasks.append(asyncio.create_task(_loop(
            30, lambda: asyncio.to_thread(runtime.get_agent("notifications").flush), "notifications")))
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.include_router(router)


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/student")
async def student_portal():
    """The Student Desk: chat with the Assistant Agent and read notifications."""
    return FileResponse(os.path.join(STATIC_DIR, "student.html"))


@app.websocket("/ws/trace")
async def ws_trace(websocket: WebSocket):
    """Live agent-to-agent message stream: a hello frame with history size,
    then the recent messages, then every new bus message as it happens."""
    await websocket.accept()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    def on_message(message: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, message)

    repo.subscribe_messages(on_message)
    try:
        history = list(reversed(repo.recent_messages(20)))
        await websocket.send_json({"type": "hello", "history": len(history)})
        for message in history:
            await websocket.send_json(message)
        while True:
            message = await queue.get()
            await websocket.send_json(message)
    except WebSocketDisconnect:
        pass
    finally:
        repo.unsubscribe_messages(on_message)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
