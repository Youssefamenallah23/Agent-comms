"""Common agent plumbing: logging, events and helpers shared by all agents."""
from .. import repo


class BaseAgent:
    name = "agent"

    def __init__(self, llm, bus):
        self.llm = llm
        self.bus = bus

    def log_event(self, event_type: str, payload: dict) -> None:
        repo.insert_event(self.name, event_type, payload)
