"""Student Assistant Agent - rule-based + LLM chat helper for campus questions."""
import json
import logging
import re

from .. import repo
from .base import BaseAgent

log = logging.getLogger("assistant")

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri"]
DAY_ORDER = {"Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5}


class AssistantAgent(BaseAgent):
    name = "assistant"

    async def chat(self, session_id: str, message: str) -> dict:
        reply, intent = self._answer_rule_based(message)
        if intent == "unknown" and self.llm.available():
            context = self._snapshot()
            prompt = (
                "You are the campus assistant of a smart campus multi-agent system. "
                "Answer the student question using ONLY the following live context. "
                "Keep it under 3 sentences.\n\n"
                f"CONTEXT: {json.dumps(context)}\n\nQUESTION: {message}"
            )
            text = self.llm.invoke(prompt)
            if text:
                reply = text.strip()
                intent = "llm"
        repo.insert_assistant_chat(session_id, "user", message)
        repo.insert_assistant_chat(session_id, "assistant", reply)
        return {"reply": reply, "intent": intent}

    async def chat_payload(self, payload: dict) -> dict:
        return await self.chat(payload.get("session_id", "web"), payload.get("message", ""))

    # ------------------------------------------------------------ intents
    def _answer_rule_based(self, message: str) -> tuple[str, str]:
        text = message.lower()
        course_match = re.search(r"\b([A-Z]{2,4}\d{3})\b", message)
        day = next((d.capitalize() for d in DAY_NAMES if d in text), None)

        if ("free" in text or "available" in text or "study" in text) and "room" in text:
            start, end = self._extract_slot(text, default=("10:00", "12:00"))
            rooms = repo.rooms_free(day or "Mon", start, end)
            if rooms:
                listing = ", ".join(f"{r['name']} (seats {r['capacity']})" for r in rooms)
                return (f"Free rooms on {day or 'Mon'} {start}-{end}: {listing}.", "free_rooms")
            return f"No rooms are free on {day or 'Mon'} {start}-{end}.", "free_rooms"

        if course_match and ("next" in text or "when" in text or "where" in text
                             or "class" in text or "schedule" in text):
            code = course_match.group(1)
            bookings = [b for b in repo.all_bookings() if b["code"] == code]
            if not bookings:
                return f"I could not find any sessions for {code}.", "course_schedule"
            upcoming = sorted(bookings, key=lambda b: (DAY_ORDER.get(b["day"], 9), b["start_time"]))[:3]
            lines = [f"{b['code']} - {b['day']} {b['start_time']}-{b['end_time']} in "
                     f"{b['room_name']} ({b['building']} building)" for b in upcoming]
            return "Next sessions: " + "; ".join(lines) + ".", "course_schedule"

        if "moved" in text or "change" in text or "relocat" in text:
            moved = [b for b in repo.all_bookings() if b["status"] == "moved"]
            if moved:
                lines = [f"{b['code']} now in {b['room_name']} (was {b['moved_from_name']}) "
                         f"on {b['day']} {b['start_time']}-{b['end_time']}" for b in moved[:5]]
                return "Recent room changes: " + "; ".join(lines) + ".", "room_changes"
            return "No rooms have been changed recently.", "room_changes"

        if "report" in text or "utilization" in text:
            rooms = repo.room_utilization()
            top = sorted(rooms, key=lambda r: -r["utilization"])[:3]
            lines = [f"{r['name']}: {r['utilization']}%" for r in top]
            return "Highest room utilization - " + ", ".join(lines) + ".", "analytics"

        return ("I can help with: free rooms ('any free rooms Wednesday?'), course "
                "schedules ('when is CS101?'), room changes, and utilization reports.",
                "unknown")

    @staticmethod
    def _extract_slot(text: str, default: tuple[str, str]) -> tuple[str, str]:
        times = re.findall(r"\b([01]?\d|2[0-3])(?:[:.](\d{2}))?\b", text)
        if len(times) >= 2:
            h1, m1 = times[0]
            h2, m2 = times[1]
            return (f"{int(h1):02d}:{m1 or '00'}", f"{int(h2):02d}:{m2 or '00'}")
        if len(times) == 1:
            h, m = times[0]
            start = f"{int(h):02d}:{m or '00'}"
            return start, repo.add_minutes(start, 120)
        return default

    def _snapshot(self) -> dict:
        rooms = repo.room_utilization()
        return {
            "rooms": [{"name": r["name"], "capacity": r["capacity"],
                       "utilization": r["utilization"]} for r in rooms],
            "conflicts_open": len(repo.open_conflicts()),
            "recent_changes": [
                {"course": b["code"], "new_room": b["room_name"], "day": b["day"]}
                for b in repo.all_bookings() if b["status"] == "moved"
            ][:5],
        }
