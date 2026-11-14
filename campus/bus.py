"""Agent-to-agent message bus with a logged protocol.

Every request/response between agents is persisted in the `messages` table
(the negotiation trace you see live on the dashboard). Transport is
pluggable: "inline" calls the registered handler in-process (default, robust
for demos and tests), "http" performs real REST calls between agents.
"""
import asyncio
import logging
import uuid

import httpx

from . import repo
from .config import settings

log = logging.getLogger("bus")


class AgentBus:
    def __init__(self):
        self._handlers: dict[tuple[str, str], object] = {}

    def register(self, agent: str, action: str, handler) -> None:
        self._handlers[(agent, action)] = handler

    def get_handler(self, agent: str, action: str):
        return self._handlers.get((agent, action))

    async def call(self, from_agent: str, to_agent: str, action: str, payload: dict,
                   correlation_id: str | None = None, topic: str | None = None) -> dict:
        cid = correlation_id or str(uuid.uuid4())
        topic = topic or f"{to_agent}.{action}"
        repo.insert_message(cid, from_agent, to_agent, "request", topic, payload)
        handler = self._handlers.get((to_agent, action))
        try:
            if handler is not None:
                if asyncio.iscoroutinefunction(handler):
                    result = await handler(payload)
                else:
                    result = handler(payload)
            elif settings.transport == "http":
                url = f"http://{settings.host}:{settings.port}/api/agents/{to_agent}/{action}"
                async with httpx.AsyncClient(timeout=60) as client:
                    response = await client.post(url, json=payload)
                    result = response.json()
            else:
                raise KeyError(f"no handler registered for {to_agent}.{action}")
            repo.insert_message(cid, to_agent, from_agent, "response", topic, result)
            return result
        except Exception as exc:
            log.error("bus call %s.%s failed: %s", to_agent, action, exc)
            error = {"error": str(exc)}
            repo.insert_message(cid, to_agent, from_agent, "error", topic, error)
            return error


bus = AgentBus()
