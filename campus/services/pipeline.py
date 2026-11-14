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
        conflict_ids = [c["id"] for c in summary["conflicts"]]
        if len(conflict_ids) > 1:
            # several conflicts at once: negotiate them jointly in one round
            batch = await coordinator.handle_conflicts_batch(conflict_ids)
            return {"scan": {"scanned_bookings": summary["scanned_bookings"],
                             "new_conflicts": summary["new_conflicts"]},
                    "batch": batch,
                    "handled": handled}
        result = await coordinator.handle_conflict(conflict_ids[0])
        handled.append({
            "conflict_id": conflict_ids[0],
            "type": summary["conflicts"][0]["type"],
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
