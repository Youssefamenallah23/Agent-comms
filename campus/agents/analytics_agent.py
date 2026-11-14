"""Analytics Agent - utilization, conflict stats and optimization advice."""
import json
import logging

from .. import repo
from .base import BaseAgent

log = logging.getLogger("analytics")


class AnalyticsAgent(BaseAgent):
    name = "analytics"

    async def report(self, payload: dict | None = None) -> dict:
        rooms = repo.room_utilization()
        stats = repo.conflict_stats()
        moved = [b for b in repo.all_bookings() if b["status"] == "moved"]

        recommendations = []
        for room in rooms:
            if room["utilization"] >= 80:
                recommendations.append(
                    f"{room['name']} runs at {room['utilization']}% utilization - "
                    f"consider opening additional sections or reallocating courses.")
            elif room["utilization"] == 0:
                recommendations.append(
                    f"{room['name']} is never used - a candidate for repurposing "
                    f"or maintenance scheduling.")
        if stats["by_type"].get("double_booking", 0) >= 2:
            recommendations.append("Recurring double bookings detected - audit the "
                                   "master timetable generation process.")
        if not recommendations:
            recommendations.append("Utilization is balanced; no structural issues detected.")

        summary = "Rule-based analytics: " + " ".join(recommendations[:2])
        if self.llm.available():
            prompt = (
                "You are the Analytics Agent of a smart campus system. Write a 3-sentence "
                "executive summary of this campus operations data. Be concrete.\n\n"
                f"DATA: {json.dumps({'rooms': rooms, 'conflicts': stats, 'moves': len(moved)})}"
            )
            text = self.llm.invoke(prompt)
            if text:
                summary = text.strip()
        self.log_event("report_generated", {"rooms": len(rooms)})
        return {
            "rooms": rooms,
            "conflicts": stats,
            "relocations": len(moved),
            "recommendations": recommendations,
            "summary": summary,
        }

    def report_payload(self, payload: dict | None = None) -> dict:
        import asyncio
        return asyncio.run(self.report(payload))
