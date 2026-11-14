"""One-click pipeline: scan for conflicts, then negotiate/resolve each one."""
import logging

from .. import repo

log = logging.getLogger("pipeline")


async def scan_and_resolve() -> dict:
    from ..runtime import get_agent

    schedule = get_agent("schedule")
    summary = await schedule.run_scan()

    handled = []
    if summary["new_conflicts"]:
        coordinator = get_agent("coordinator")
        for conflict in summary["conflicts"]:
            result = await coordinator.handle_conflict(conflict["id"])
            handled.append({
                "conflict_id": conflict["id"],
                "type": conflict["type"],
                "decision": result["decision"],
                "best": result.get("best") or {},
                "moved_booking_id": result.get("moved_booking_id"),
            })
    return {"scan": {"scanned_bookings": summary["scanned_bookings"],
                     "new_conflicts": summary["new_conflicts"]},
            "handled": handled}


async def resolve_open_conflicts() -> list[dict]:
    """Handle conflicts that are still open (e.g. left pending from earlier runs)."""
    from ..runtime import get_agent

    coordinator = get_agent("coordinator")
    results = []
    for conflict in repo.open_conflicts():
        result = await coordinator.handle_conflict(conflict["id"])
        results.append({"conflict_id": conflict["id"], "decision": result["decision"]})
    return results
