"""Schedule Agent - monitors the timetable and detects conflicts.

LangGraph pipeline:  scan -> detect -> (analyze via LLM) -> publish
"""
import json
import logging
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .. import repo
from ..services import conflicts as conflicts_svc
from .base import BaseAgent

log = logging.getLogger("schedule")


class ScheduleState(TypedDict, total=False):
    bookings: list
    new_conflicts: list
    summaries: list


def _brief(b: dict) -> dict:
    return {
        "booking_id": b["id"], "course": b["code"], "title": b["title"],
        "instructor": b["instructor"], "enrolled": b["enrolled"], "priority": b["priority"],
        "needs": b["required_equipment"], "room": b["room_name"], "capacity": b["capacity"],
        "day": b["day"], "start": b["start_time"], "end": b["end_time"],
    }


def heuristic_analysis(conflict: dict, affected: list[dict]) -> dict:
    names = ", ".join(_brief(b)["course"] for b in affected)
    return {
        "severity": conflict["severity"],
        "explanation": f"{conflict['description']} Affected classes: {names}.",
        "recommendation": (
            "Reallocate the lower-priority class to a free room with adequate "
            "capacity and equipment before the session starts."
        ),
    }


class ScheduleAgent(BaseAgent):
    name = "schedule"

    def __init__(self, llm, bus):
        super().__init__(llm, bus)
        self.graph = self._build_graph()

    # ------------------------------------------------------------ nodes
    async def scan_node(self, state: ScheduleState) -> dict:
        bookings = repo.all_bookings()
        self.log_event("scan", {"active_bookings": len(bookings)})
        return {"bookings": bookings}

    async def detect_node(self, state: ScheduleState) -> dict:
        new_conflicts = conflicts_svc.detect_all()
        if new_conflicts:
            self.log_event("conflicts_detected", {"count": len(new_conflicts)})
        return {"new_conflicts": new_conflicts}

    async def analyze_node(self, state: ScheduleState) -> dict:
        summaries = []
        for conflict in state.get("new_conflicts", []):
            affected = [
                repo.get_booking_full(conflict["booking_a_id"]),
                *( [repo.get_booking_full(conflict["booking_b_id"])]
                   if conflict["booking_b_id"] else [] ),
            ]
            affected = [b for b in affected if b]
            analysis = heuristic_analysis(conflict, affected)
            if self.llm.available():
                prompt = (
                    "You are the Schedule Agent of a smart campus multi-agent system. "
                    "A timetable conflict was detected. Classify it and explain it briefly.\n\n"
                    f"CONFLICT: {json.dumps({k: conflict[k] for k in ('type', 'severity', 'description')})}\n"
                    f"AFFECTED CLASSES: {json.dumps([_brief(b) for b in affected])}\n\n"
                    'Reply ONLY with JSON: {"severity": "low|medium|high", '
                    '"explanation": "<max 2 sentences>", '
                    '"recommendation": "<max 2 sentences, actionable>"}'
                )
                data = self.llm.invoke_json(prompt)
                if data and data.get("explanation"):
                    analysis = {
                        "severity": data.get("severity", conflict["severity"]),
                        "explanation": data["explanation"],
                        "recommendation": data.get("recommendation", ""),
                        "by": "llm",
                    }
            repo.set_conflict_analysis(conflict["id"], json.dumps(analysis))
            summaries.append({"conflict_id": conflict["id"], "analysis": analysis})
        return {"summaries": summaries}

    async def publish_node(self, state: ScheduleState) -> dict:
        for conflict in state.get("new_conflicts", []):
            self.log_event("conflict_published", {
                "conflict_id": conflict["id"],
                "type": conflict["type"],
                "severity": conflict["severity"],
            })
        return {}

    # ------------------------------------------------------------ graph
    def _build_graph(self):
        g = StateGraph(ScheduleState)
        g.add_node("scan", self.scan_node)
        g.add_node("detect", self.detect_node)
        g.add_node("analyze", self.analyze_node)
        g.add_node("publish", self.publish_node)
        g.add_edge(START, "scan")
        g.add_edge("scan", "detect")
        g.add_conditional_edges(
            "detect",
            lambda s: "analyze" if s.get("new_conflicts") else "publish",
            {"analyze": "analyze", "publish": "publish"},
        )
        g.add_edge("analyze", "publish")
        g.add_edge("publish", END)
        return g.compile()

    async def run_scan(self) -> dict:
        final = await self.graph.ainvoke({})
        new_conflicts = final.get("new_conflicts", [])
        return {
            "scanned_bookings": len(final.get("bookings", [])),
            "new_conflicts": len(new_conflicts),
            "conflicts": new_conflicts,
            "summaries": final.get("summaries", []),
        }
