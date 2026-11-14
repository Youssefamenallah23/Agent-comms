"""FastAPI routes - public API, agent endpoints and simulation controls."""
import asyncio
import csv
import io
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from .. import repo, runtime
from .. import auth as campus_auth
from ..config import settings
from ..models import (AvailabilityQuery, BookingRequest, ChatRequest, EnrollRequest,
                      InjectRequest, LoginRequest, RegisterRequest, RoomRequest,
                      RoomStatusRequest, SeedRequest)
from ..services import pipeline
from ..simulation import seed

router = APIRouter(prefix="/api")

_VALID_DAYS = {"Mon", "Tue", "Wed", "Thu", "Fri"}


def _validate_slot(day: str, start: str, end: str) -> None:
    if day not in _VALID_DAYS:
        raise HTTPException(status_code=422,
                            detail=f"day must be one of {sorted(_VALID_DAYS)}")
    try:
        if not (8 * 60 <= repo.minutes(start) < repo.minutes(end) <= 18 * 60):
            raise ValueError
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=422,
                            detail="times must be HH:MM inside 08:00-18:00, start before end")


async def _availability_for_course(course: dict, day: str, start: str, end: str,
                                   exclude_room_ids=()) -> dict:
    """Ask the Availability Agent for ranked rooms that satisfy a course's needs."""
    return await runtime.get_agent("availability").run_query({
        "day": day,
        "start_time": start,
        "end_time": end,
        "min_capacity": course["enrolled"],
        "required_equipment": course["required_equipment"],
        "exclude_room_ids": list(exclude_room_ids),
        "prefer_building": None,
        "instructor": course["instructor"],
        "exclude_booking_ids": [],
        "context": {
            "enrolled": course["enrolled"],
            "required_equipment": course["required_equipment"],
            "current_building": None,
            "current_slot": {"day": day, "start": start, "end": end},
        },
    })


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


# ------------------------------------------------------------------ campus life
@router.post("/bookings", status_code=201)
async def create_booking(request: BookingRequest):
    """Book a room for a course. Hard constraints are validated first; if the
    request cannot be honoured, the Availability Agent proposes ranked
    alternatives instead of a blunt refusal."""
    course = repo.get_course_by_code(request.course_code.strip().upper())
    if not course:
        raise HTTPException(status_code=404, detail=f"unknown course {request.course_code}")
    room_row = repo.query_one("SELECT id FROM rooms WHERE name = ?", (request.room_name,))
    if not room_row:
        raise HTTPException(status_code=404, detail=f"unknown room {request.room_name}")
    room = repo.get_room(room_row["id"])
    _validate_slot(request.day, request.start_time, request.end_time)

    problems = []
    if room["status"] != "active":
        problems.append("room_under_maintenance")
    if course["enrolled"] > room["capacity"]:
        problems.append("capacity_overflow")
    missing = [e for e in course["required_equipment"] if e not in room["equipment"]]
    if missing:
        problems.append("missing_equipment: " + ", ".join(missing))
    if not repo.room_free(room["id"], request.day, request.start_time, request.end_time):
        problems.append("room_busy")
    if not repo.instructor_free(course["instructor"], request.day,
                                request.start_time, request.end_time):
        problems.append("instructor_busy")

    if problems:
        alternatives = (await _availability_for_course(
            course, request.day, request.start_time, request.end_time))["candidates"]
        raise HTTPException(status_code=409, detail={
            "problems": problems,
            "alternatives": alternatives[:5],
        })

    booking_id = repo.insert_booking(course["id"], room["id"], request.day,
                                     request.start_time, request.end_time, "api")
    scan = await runtime.get_agent("schedule").run_scan()
    return {
        "booking_id": booking_id,
        "status": "confirmed",
        "room": room["name"],
        "course": course["code"],
        "new_conflicts_after_scan": scan["new_conflicts"],
    }


@router.post("/bookings/{booking_id}/cancel")
async def cancel_booking_endpoint(booking_id: int):
    booking = repo.get_booking_full(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="booking not found")
    if booking["status"] == "cancelled":
        raise HTTPException(status_code=409, detail="booking already cancelled")
    repo.cancel_booking(booking_id)
    scan = await runtime.get_agent("schedule").run_scan()
    return {
        "cancelled": True,
        "course": booking["code"],
        "room": booking["room_name"],
        "new_conflicts_after_scan": scan["new_conflicts"],
    }


@router.post("/requests", status_code=201)
async def room_request(request: RoomRequest):
    """A student asks for a room for a course. If a strong fit exists at the
    requested slot it is booked immediately; otherwise the agents propose
    time-shifted alternatives for confirmation instead of refusing."""
    course = repo.get_course_by_code(request.course_code.strip().upper())
    if not course:
        raise HTTPException(status_code=404, detail=f"unknown course {request.course_code}")
    _validate_slot(request.day, request.start_time, request.end_time)

    candidates = (await _availability_for_course(
        course, request.day, request.start_time, request.end_time))["candidates"]

    if candidates and candidates[0]["score"] >= settings.auto_apply_threshold:
        best = candidates[0]
        booking_id = repo.insert_booking(course["id"], best["room_id"], request.day,
                                         request.start_time, request.end_time,
                                         "student-request")
        scan = await runtime.get_agent("schedule").run_scan()
        return {
            "status": "confirmed",
            "booking_id": booking_id,
            "room": best["room_name"],
            "score": best["score"],
            "alternatives": candidates[1:4],
            "new_conflicts_after_scan": scan["new_conflicts"],
        }

    proposals = [{"room": c["room_name"], "day": request.day,
                  "start": request.start_time, "end": request.end_time,
                  "score": c["score"]} for c in candidates[:3]]
    for shift in (60, -60, 120, -120):
        shifted_start = repo.add_minutes(request.start_time, shift)
        shifted_end = repo.add_minutes(request.end_time, shift)
        if shifted_start < "08:00" or shifted_end > "18:00":
            continue
        shifted = (await _availability_for_course(
            course, request.day, shifted_start, shifted_end))["candidates"]
        for c in shifted[:2]:
            proposals.append({"room": c["room_name"], "day": request.day,
                              "start": shifted_start, "end": shifted_end,
                              "score": c["score"]})
    if not proposals:
        return {"status": "unfulfillable", "proposals": []}
    proposals.sort(key=lambda p: -p["score"])
    return {"status": "needs_confirmation", "proposals": proposals[:6]}


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


@router.get("/agents/assistant/history")
async def assistant_history(session_id: str = "web", limit: int = 50):
    """Restore a previous conversation so the Student Desk survives a reload."""
    return repo.assistant_history(session_id, limit)


@router.get("/notifications")
async def notifications_feed(limit: int = 30):
    """Public notifications feed for the Student Desk portal."""
    return repo.recent_notifications(limit)


# ------------------------------------------------------------------ student accounts
def _clean(student) -> dict:
    return {"id": student["id"], "name": student["name"], "email": student["email"]}


def _current_student(authorization: str = Header(default="")):
    token = authorization.replace("Bearer", "").strip()
    student = repo.student_for_token(token) if token else None
    if not student:
        raise HTTPException(status_code=401, detail="sign in required")
    return student


@router.post("/auth/register", status_code=201)
def register(request: RegisterRequest):
    email = request.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="invalid email")
    if len(request.password) < 8:
        raise HTTPException(status_code=422, detail="password must be at least 8 characters")
    if repo.get_student_by_email(email):
        raise HTTPException(status_code=409, detail="email already registered")
    student_id = repo.create_student(request.name.strip(), email,
                                     campus_auth.hash_password(request.password))
    token = campus_auth.new_token()
    repo.create_auth_token(student_id, token)
    return {"token": token, "student": _clean(repo.get_student(student_id))}


@router.post("/auth/login")
def login(request: LoginRequest):
    student = repo.get_student_by_email(request.email.strip().lower())
    if not student or not campus_auth.verify_password(request.password,
                                                      student["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid email or password")
    token = campus_auth.new_token()
    repo.create_auth_token(student["id"], token)
    return {"token": token, "student": _clean(student)}


@router.get("/auth/me")
def me(student=Depends(_current_student)):
    return {**_clean(student), "courses": repo.my_courses(student["id"])}


@router.post("/auth/enroll")
def enroll(request: EnrollRequest, student=Depends(_current_student)):
    course = repo.get_course_by_code(request.course_code.strip().upper())
    if not course:
        raise HTTPException(status_code=404, detail=f"unknown course {request.course_code}")
    repo.enroll(student["id"], course["code"])
    return {"enrolled": course["code"], "courses": repo.my_courses(student["id"])}


@router.get("/auth/my/notifications")
def my_notifications(student=Depends(_current_student)):
    return repo.my_notifications(student["id"])


@router.get("/auth/my/schedule")
def my_schedule(student=Depends(_current_student)):
    codes = set(repo.my_courses(student["id"]))
    day_order = {"Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5}
    bookings = [b for b in repo.all_bookings() if b["code"] in codes]
    bookings.sort(key=lambda b: (day_order.get(b["day"], 9), b["start_time"]))
    return [{"course": b["code"], "title": b["title"], "instructor": b["instructor"],
             "day": b["day"], "start": b["start_time"], "end": b["end_time"],
             "room": b["room_name"], "building": b["building"],
             "moved": b["status"] == "moved"} for b in bookings]


@router.get("/courses")
def list_courses():
    return repo.all_courses()


@router.get("/agents/analytics/report")
async def analytics_report():
    return await runtime.get_agent("analytics").report()


@router.get("/agents/analytics/report.csv")
async def analytics_report_csv():
    """Utilization report as CSV for spreadsheets and archiving."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["room", "building", "capacity", "booked_hours",
                     "utilization_percent", "status"])
    for room in repo.room_utilization():
        writer.writerow([room["name"], room["building"], room["capacity"],
                         room["booked_hours"], room["utilization"], room["status"]])
    return Response(content=buffer.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=campus-utilization.csv"})


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


# ------------------------------------------------------------------ campus ops
@router.post("/rooms/{room_id}/maintenance")
async def room_maintenance(room_id: int, request: RoomStatusRequest):
    """Take a room offline for maintenance (or bring it back). Bookings in a
    maintenance room are flagged as room_unavailable conflicts on the next
    scan and the agents relocate them automatically."""
    room = repo.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="room not found")
    repo.set_room_status(room_id, request.status)
    runtime.get_agent("schedule").log_event(
        "room_status_changed", {"room": room["name"], "status": request.status})
    return {"room": room["name"], "status": request.status,
            "affected": len([b for b in repo.all_bookings()
                             if b["room_id"] == room_id and b["status"] != "cancelled"])}


@router.get("/agents/status")
async def agents_status():
    """Per-agent heartbeat: events logged and bus messages sent/received."""
    roles = {
        "schedule": "Scans the timetable and detects conflicts",
        "availability": "Finds and ranks free rooms for requests",
        "coordinator": "Negotiates reallocations and applies policy",
        "notifications": "Delivers alerts to students, instructors and admins",
        "assistant": "Answers student questions about the campus",
        "analytics": "Reports utilization and recommends optimizations",
    }
    events = {r["agent"]: r["events"] for r in
              repo.query("SELECT agent, COUNT(*) AS events FROM events_log GROUP BY agent")}
    sent = {r["agent"]: r["sent"] for r in
            repo.query("SELECT from_agent AS agent, COUNT(*) AS sent FROM messages GROUP BY from_agent")}
    received = {r["agent"]: r["received"] for r in
                repo.query("SELECT to_agent AS agent, COUNT(*) AS received FROM messages GROUP BY to_agent")}
    agents = [{
        "name": name,
        "role": role,
        "events": events.get(name, 0),
        "messages_sent": sent.get(name, 0),
        "messages_received": received.get(name, 0),
    } for name, role in roles.items()]
    return {"llm": runtime.llm_status(), "agents": agents}
