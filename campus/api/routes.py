"""FastAPI routes - public API, agent endpoints and simulation controls."""
import asyncio
import json

from fastapi import APIRouter, HTTPException

from .. import repo, runtime
from ..models import AvailabilityQuery, ChatRequest, InjectRequest, SeedRequest
from ..services import pipeline
from ..simulation import seed

router = APIRouter(prefix="/api")


# ------------------------------------------------------------------ system
@router.get("/health")
async def health():
    return {"status": "ok", "llm": runtime.llm_status()}


@router.get("/state")
async def state():
    conflicts = []
    for c in repo.all_conflicts(30):
        props = repo.proposals_for(c["id"])
        best = props[0] if props else None
        best_view = None
        if best:
            room = repo.get_room(best["new_room_id"]) if best["new_room_id"] else None
            best_view = {
                "id": best["id"], "score": best["score"], "status": best["status"],
                "reasoning": best["reasoning"], "round": best["round"],
                "new_room": room["name"] if room else None,
                "new_day": best["new_day"], "new_start": best["new_start"],
                "new_end": best["new_end"],
            }
        analysis = None
        if c["llm_analysis"]:
            try:
                analysis = json.loads(c["llm_analysis"])
            except Exception:
                analysis = {"explanation": c["llm_analysis"]}
        conflicts.append({**c, "analysis": analysis, "best_proposal": best_view})
    moved = [b for b in repo.all_bookings() if b["status"] == "moved"]
    return {
        "stats": {
            "rooms": len(repo.all_rooms()),
            "courses": len(repo.all_courses()),
            "bookings": len(repo.all_bookings()),
            "conflicts_open": len(repo.open_conflicts()),
            "conflicts_resolved": repo.conflict_stats()["by_status"].get("resolved", 0),
            "pending_approvals": repo.conflict_stats()["by_status"].get("pending_approval", 0),
            "auto_resolved": len([p for p in _all_applied_proposals()]),
            "notifications_sent": len(repo.recent_notifications(1000)),
            "relocations": len(moved),
            "llm": runtime.llm_status(),
        },
        "conflicts": conflicts,
        "messages": repo.recent_messages(40),
        "notifications": repo.recent_notifications(15),
        "rooms": repo.room_utilization(),
        "moved": [
            {"code": b["code"], "from": b["moved_from_name"], "to": b["room_name"],
             "day": b["day"], "start": b["start_time"], "end": b["end_time"]}
            for b in moved
        ],
    }


def _all_applied_proposals():
    return repo.query("SELECT id FROM proposals WHERE status = 'applied'")


# ------------------------------------------------------------------ pipeline
@router.post("/pipeline/scan-and-resolve")
async def run_pipeline():
    return await pipeline.scan_and_resolve()


# ------------------------------------------------------------------ simulation
@router.post("/simulate/seed")
async def seed_route(request: SeedRequest):
    created = seed.seed_data(force=request.force)
    return {"seeded": created}


@router.post("/simulate/inject")
async def inject_route(request: InjectRequest):
    return seed.inject_conflict(request.kind)


# ------------------------------------------------------------------ agents
@router.post("/agents/schedule/scan")
async def schedule_scan():
    return await runtime.get_agent("schedule").run_scan()


@router.post("/agents/availability/query")
async def availability_query(request: AvailabilityQuery):
    return await runtime.get_agent("availability").run_query(request.model_dump())


@router.post("/agents/coordinator/handle/{conflict_id}")
async def coordinator_handle(conflict_id: int):
    conflict = repo.get_conflict(conflict_id)
    if not conflict:
        raise HTTPException(status_code=404, detail="conflict not found")
    return await runtime.get_agent("coordinator").handle_conflict(conflict_id)


@router.post("/agents/notifications/run")
async def notifications_run():
    return {"sent": runtime.get_agent("notifications").flush()}


@router.post("/agents/assistant/chat")
async def assistant_chat(request: ChatRequest):
    return await runtime.get_agent("assistant").chat(request.session_id, request.message)


@router.get("/agents/analytics/report")
async def analytics_report():
    return await runtime.get_agent("analytics").report()


# generic agent endpoint (used by the http transport of the message bus)
@router.post("/agents/{agent_name}/{action}")
async def agent_call(agent_name: str, action: str, payload: dict | None = None):
    handler = runtime.get_handler(agent_name, action)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"unknown agent action {agent_name}.{action}")
    if asyncio.iscoroutinefunction(handler):
        return await handler(payload or {})
    return handler(payload or {})


# ------------------------------------------------------------------ conflicts & decisions
@router.get("/conflicts")
async def conflicts_list():
    return repo.all_conflicts(100)


@router.get("/conflicts/{conflict_id}")
async def conflict_detail(conflict_id: int):
    conflict = repo.get_conflict(conflict_id)
    if not conflict:
        raise HTTPException(status_code=404, detail="conflict not found")
    return {**conflict,
            "proposals": repo.proposals_for(conflict_id),
            "messages": repo.messages_like(f"cf-{conflict_id}-%", 200)}


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: int):
    from ..agents.coordinator_agent import apply_proposal

    proposal = repo.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    result = apply_proposal(proposal_id)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result.get("reason", "cannot apply"))
    conflict = repo.get_conflict(proposal["conflict_id"])
    moved = repo.get_booking_full(proposal["booking_id"])
    room = repo.get_room(proposal["new_room_id"])
    repo.insert_notification(
        f"students:{moved['code']}",
        f"Room change for {moved['code']} (approved)",
        f"{moved['code']} on {proposal['new_day']} {proposal['new_start']}-"
        f"{proposal['new_end']} now takes place in {room['name']} "
        f"(was {moved['moved_from_name'] or moved['room_name']}).",
        conflict["id"],
    )
    runtime.get_agent("notifications").flush()
    return {"approved": True, "conflict_id": conflict["id"],
            "moved_booking_id": proposal["booking_id"]}


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal_route(proposal_id: int):
    from ..agents.coordinator_agent import reject_proposal

    proposal = repo.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    result = reject_proposal(proposal_id)
    repo.insert_notification(
        "admin",
        f"Proposal #{proposal_id} rejected",
        "The proposed reallocation was rejected by a human operator. "
        "The conflict stays escalated until handled manually.",
        proposal["conflict_id"],
    )
    runtime.get_agent("notifications").flush()
    return result


@router.get("/messages")
async def messages(correlation_id: str | None = None):
    if correlation_id:
        return repo.messages_like(correlation_id.replace("%", "") + "%", 300)
    return repo.recent_messages(100)
