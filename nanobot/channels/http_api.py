"""HTTP API channel using FastAPI."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.session.manager import SessionManager


@dataclass
class HttpApiConfig:
    """HTTP API channel configuration."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    allow_from: list[str] = field(default_factory=lambda: ["*"])


class ChatRequest(BaseModel):
    user_id: str
    message: str
    session_id: str = "default"


class HttpApiChannel(BaseChannel):
    name = "http"

    def __init__(self, config: HttpApiConfig, bus: MessageBus, session_manager: SessionManager | None = None):
        super().__init__(config, bus)
        self._session_manager = session_manager
        # Map request_id -> asyncio.Queue to correlate responses
        self._pending: dict[str, asyncio.Queue[OutboundMessage]] = {}
        self._server: uvicorn.Server | None = None
        self.app = self._build_app()

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Nanobot API", version="1.0.0")

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/api/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.post("/api/chat")
        async def chat(req: ChatRequest) -> dict[str, Any]:
            """Send a message and wait for the agent's response."""
            if not req.user_id.strip():
                raise HTTPException(status_code=400, detail="user_id is required")
            if not req.message.strip():
                raise HTTPException(status_code=400, detail="message is required")

            response = await self._process(req.user_id.strip(), req.message.strip(), req.session_id.strip())
            return {
                "user_id": req.user_id,
                "message": response,
                "session_key": f"http:{req.user_id}:{req.session_id}",
            }

        @app.post("/api/chat/stream")
        async def chat_stream(req: ChatRequest) -> StreamingResponse:
            """Send a message and stream the agent's response via SSE."""
            if not req.user_id.strip():
                raise HTTPException(status_code=400, detail="user_id is required")
            if not req.message.strip():
                raise HTTPException(status_code=400, detail="message is required")

            return StreamingResponse(
                self._stream(req.user_id.strip(), req.message.strip(), req.session_id.strip()),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        @app.get("/api/users")
        async def list_users() -> list[dict]:
            if not self._session_manager:
                raise HTTPException(status_code=503, detail="Session manager not available")
            return self._session_manager.list_users()

        @app.post("/api/users")
        async def create_user(body: dict) -> dict:
            if not self._session_manager:
                raise HTTPException(status_code=503, detail="Session manager not available")
            user_id = (body.get("user_id") or "").strip()
            if not user_id:
                raise HTTPException(status_code=400, detail="user_id is required")
            return self._session_manager.create_user(user_id)

        @app.get("/api/sessions")
        async def list_sessions(user_id: str) -> list[dict]:
            if not self._session_manager:
                raise HTTPException(status_code=503, detail="Session manager not available")
            return self._session_manager.list_sessions(user_id=user_id)

        @app.get("/api/sessions/messages")
        async def get_messages(key: str) -> list[dict]:
            if not self._session_manager:
                raise HTTPException(status_code=503, detail="Session manager not available")
            return self._session_manager.get_session_messages(key)

        @app.delete("/api/sessions")
        async def delete_session(key: str) -> dict:
            if not self._session_manager:
                raise HTTPException(status_code=503, detail="Session manager not available")
            self._session_manager.delete_session(key)
            return {"ok": True}

        return app

    async def _process(self, user_id: str, message: str, session_id: str = "default", timeout: float = 180.0) -> str:
        """Publish message to bus and wait for the final response."""
        request_id = str(uuid.uuid4())
        queue: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._pending[request_id] = queue

        try:
            await self._handle_message(
                sender_id=user_id,
                chat_id=request_id,
                content=message,
                session_key=f"http:{user_id}:{session_id}",
            )
            # Drain queue until we get a non-progress (final) message
            while True:
                msg = await asyncio.wait_for(queue.get(), timeout=timeout)
                if not msg.metadata.get("_progress"):
                    return msg.content
        except asyncio.TimeoutError:
            logger.error("HTTP channel: request {} timed out", request_id)
            raise HTTPException(status_code=504, detail="Agent response timed out")
        finally:
            self._pending.pop(request_id, None)

    async def _stream(
        self, user_id: str, message: str, session_id: str = "default", timeout: float = 180.0
    ) -> AsyncGenerator[str, None]:
        """Publish message to bus and stream all outbound messages as SSE."""
        request_id = str(uuid.uuid4())
        queue: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._pending[request_id] = queue

        try:
            await self._handle_message(
                sender_id=user_id,
                chat_id=request_id,
                content=message,
                session_key=f"http:{user_id}:{session_id}",
            )

            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=timeout)
                    is_progress = msg.metadata.get("_progress", False)
                    data = json.dumps(
                        {
                            "content": msg.content,
                            "done": not is_progress,
                            "tool_hint": msg.metadata.get("_tool_hint", False),
                        },
                        ensure_ascii=False,
                    )
                    yield f"data: {data}\n\n"
                    if not is_progress:
                        break
                except asyncio.TimeoutError:
                    yield "data: " + json.dumps({"error": "timeout", "done": True}) + "\n\n"
                    break
        finally:
            self._pending.pop(request_id, None)

    async def send(self, msg: OutboundMessage) -> None:
        """Called by ChannelManager to deliver agent response."""
        queue = self._pending.get(msg.chat_id)
        if queue is not None:
            await queue.put(msg)

    async def start(self) -> None:
        self._running = True
        logger.info("HTTP API channel starting on {}:{}", self.config.host, self.config.port)
        config = uvicorn.Config(
            self.app,
            host=self.config.host,
            port=self.config.port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)
        await self._server.serve()

    async def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.should_exit = True
