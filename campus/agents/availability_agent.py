"""Availability Agent - finds and ranks free rooms for a request.

LangGraph pipeline:  parse -> query -> rank
Exposes a single service action: query(day, start, end, min_capacity,
required_equipment, exclude_room_ids, prefer_building, context).
"""
import logging
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .. import repo
from ..services import scoring
from .base import BaseAgent

log = logging.getLogger("availability")


class AvailabilityState(TypedDict, total=False):
    request: dict
    candidates: list
    ranked: list


class AvailabilityAgent(BaseAgent):
    name = "availability"

    def __init__(self, llm, bus):
        super().__init__(llm, bus)
        self.graph = self._build_graph()

    # ------------------------------------------------------------ nodes
    async def parse_node(self, state: AvailabilityState) -> dict:
        r = state["request"]
        request = {
            "day": r.get("day", "Mon"),
            "start_time": r.get("start_time", "08:00"),
            "end_time": r.get("end_time", "10:00"),
            "min_capacity": int(r.get("min_capacity", 1)),
            "required_equipment": list(r.get("required_equipment", [])),
            "exclude_room_ids": list(r.get("exclude_room_ids", [])),
            "prefer_building": r.get("prefer_building"),
            "instructor": r.get("instructor"),
            "exclude_booking_ids": list(r.get("exclude_booking_ids", [])),
            "context": r.get("context") or {},
        }
        self.log_event("query_parsed", request)
        return {"request": request}

    async def query_node(self, state: AvailabilityState) -> dict:
        r = state["request"]
        rooms = repo.rooms_free(r["day"], r["start_time"], r["end_time"], r["exclude_room_ids"])
        candidates = []
        for room in rooms:
            if room["capacity"] < r["min_capacity"]:
                continue
            if any(e not in room["equipment"] for e in r["required_equipment"]):
                continue
            candidates.append(room)
        # instructor must also be free at the requested slot (ignoring the
        # bookings that are themselves being rescheduled)
        instructor = r.get("instructor")
        if instructor:
            excluded = set(r.get("exclude_booking_ids", []))
            candidates = [
                room for room in candidates
                if not any(
                    b["instructor"] == instructor
                    and b["id"] not in excluded
                    and repo.overlaps(b["start_time"], b["end_time"],
                                      r["start_time"], r["end_time"])
                    for b in repo.all_bookings() if b["day"] == r["day"]
                )
            ]
        return {"candidates": candidates}

    async def rank_node(self, state: AvailabilityState) -> dict:
        r = state["request"]
        ctx = dict(r["context"])
        ctx.setdefault("current_slot", {"day": r["day"], "start": r["start_time"], "end": r["end_time"]})
        ranked = []
        for room in state["candidates"]:
            slot = {"day": r["day"], "start": r["start_time"], "end": r["end_time"]}
            score, breakdown = scoring.score_candidate(ctx, room, slot, r["prefer_building"])
            ranked.append({
                "room_id": room["id"],
                "room_name": room["name"],
                "building": room["building"],
                "capacity": room["capacity"],
                "equipment": room["equipment"],
                "score": score,
                "breakdown": breakdown,
                "slot": slot,
            })
        ranked.sort(key=lambda c: (-c["score"], c["room_id"]))
        return {"ranked": ranked}

    # ------------------------------------------------------------ graph
    def _build_graph(self):
        g = StateGraph(AvailabilityState)
        g.add_node("parse", self.parse_node)
        g.add_node("query", self.query_node)
        g.add_node("rank", self.rank_node)
        g.add_edge(START, "parse")
        g.add_edge("parse", "query")
        g.add_edge("query", "rank")
        g.add_edge("rank", END)
        return g.compile()

    async def run_query(self, payload: dict) -> dict:
        final = await self.graph.ainvoke({"request": payload})
        ranked = final.get("ranked", [])
        self.log_event("query_answered", {"candidates": len(ranked)})
        return {"candidates": ranked}
