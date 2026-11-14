"""Tests for the Smart Campus Multi-Agent System (rule-based mode, no network)."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from campus.agents.coordinator_agent import apply_proposal
from campus.db import repo
from campus.main import app
from campus.runtime import get_agent, setup
from campus.services import scoring
from campus.services.pipeline import scan_and_resolve
from campus.simulation import seed


@pytest.fixture(scope="session", autouse=True)
def prepared():
    setup()
    repo.init_db()
    repo.clear_all_data()
    seed.seed_data(force=True)
    yield


def test_seed_creates_expected_data():
    assert len(repo.all_rooms()) == 7
    assert len(repo.all_courses()) == 11
    assert len(repo.all_bookings()) == 23


def test_scoring_prefers_tight_fit():
    ctx = {"enrolled": 45, "required_equipment": ["projector"], "current_building": "A",
           "current_slot": {"day": "Wed", "start": "10:00", "end": "12:00"}}
    slot = {"day": "Wed", "start": "10:00", "end": "12:00"}
    auditorium = {"id": 1, "name": "Aud", "building": "A", "capacity": 150,
                  "equipment": ["projector"]}
    right_size = {"id": 2, "name": "C-105", "building": "C", "capacity": 50,
                  "equipment": ["projector"]}
    score_big, _ = scoring.score_candidate(ctx, auditorium, slot, prefer_building="A")
    score_fit, _ = scoring.score_candidate(ctx, right_size, slot, prefer_building="A")
    assert score_fit > score_big


def test_scan_detects_seeded_conflict():
    summary = asyncio.run(get_agent("schedule").run_scan())
    assert summary["new_conflicts"] >= 1
    assert "double_booking" in {c["type"] for c in summary["conflicts"]}


def test_coordinator_resolves_seeded_conflict_end_to_end():
    open_conflicts = [c for c in repo.open_conflicts() if c["type"] == "double_booking"]
    assert open_conflicts, "seeded double booking must be detected"
    conflict = open_conflicts[0]
    result = asyncio.run(get_agent("coordinator").handle_conflict(conflict["id"]))

    assert result["decision"] == "auto_applied"
    moved = repo.get_booking_full(result["moved_booking_id"])
    assert moved["status"] == "moved"
    assert moved["room_name"] == "C-105"
    assert moved["moved_from_name"] == "Auditorium-A"
    assert repo.get_conflict(conflict["id"])["status"] == "resolved"

    # the surviving class keeps its room
    other_id = conflict["booking_a_id"] if conflict["booking_b_id"] == moved["id"] \
        else conflict["booking_b_id"]
    other = repo.get_booking_full(other_id)
    assert other["room_name"] == "Auditorium-A"

    # agent-to-agent messages were logged and notifications dispatched
    assert len(repo.messages_like(f"cf-{conflict['id']}-%")) >= 6
    assert len(repo.recent_notifications(50)) >= 2


def test_capacity_conflict_escalates_when_no_safe_option():
    """Inject a capacity overflow: CS101 (60) into B-201 (40) on Mon 08:00.
    The only big room (Auditorium-A) is busy at that slot and the instructor
    teaches CS340 Mon 10-12, so a correct agent must NOT apply a broken move."""
    course = repo.get_course_by_code("CS101")
    room = repo.query_one("SELECT * FROM rooms WHERE name = 'B-201'")
    booking_id = repo.insert_booking(course["id"], room["id"], "Mon", "08:00", "10:00", "test")
    summary = asyncio.run(get_agent("schedule").run_scan())
    types = {c["type"] for c in summary["conflicts"]}
    assert "capacity_overflow" in types

    conflict = next(c for c in summary["conflicts"]
                    if c["type"] == "capacity_overflow" and c["booking_a_id"] == booking_id)
    result = asyncio.run(get_agent("coordinator").handle_conflict(conflict["id"]))
    # the coordinator must not create an instructor clash while fixing capacity
    assert result["decision"] == "failed"
    assert repo.get_conflict(conflict["id"])["status"] == "escalated"


def test_exam_conflict_requires_human_approval_then_applies():
    """Double-book the exam slot (MID-PH, priority=exam). Exams never auto-apply,
    so the best proposal must wait for a human - then the approval endpoint
    applies it."""
    course = repo.get_course_by_code("HIST105")
    room = repo.query_one("SELECT * FROM rooms WHERE name = 'Auditorium-A'")
    booking_id = repo.insert_booking(course["id"], room["id"], "Fri", "14:00", "16:00", "test")
    summary = asyncio.run(get_agent("schedule").run_scan())
    conflict = next(c for c in summary["conflicts"]
                    if c["type"] == "double_booking" and booking_id in (c["booking_a_id"], c["booking_b_id"]))
    result = asyncio.run(get_agent("coordinator").handle_conflict(conflict["id"]))
    assert result["decision"] == "pending_approval"
    assert repo.get_conflict(conflict["id"])["status"] == "pending_approval"
    proposal = repo.proposals_for(conflict["id"])[0]
    assert proposal["status"] == "awaiting_approval"

    client = TestClient(app)
    response = client.post(f"/api/proposals/{proposal['id']}/approve")
    assert response.status_code == 200
    assert repo.get_proposal(proposal["id"])["status"] == "applied"
    moved = repo.get_booking_full(booking_id)
    assert moved["status"] == "moved" and moved["room_name"] == "B-201"
    assert repo.get_conflict(conflict["id"])["status"] == "resolved"


def test_full_pipeline_via_api():
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        injected = client.post("/api/simulate/inject",
                               json={"kind": "equipment_mismatch"}).json()
        assert injected["injected"] == "equipment_mismatch"
        data = client.post("/api/pipeline/scan-and-resolve").json()
        assert data["scan"]["new_conflicts"] >= 1
        assert len(data["handled"]) >= 1
        state = client.get("/api/state").json()
        assert state["stats"]["rooms"] == 7
        assert state["stats"]["conflicts_resolved"] >= 1
        report = client.get("/api/agents/analytics/report").json()
        assert "rooms" in report and "summary" in report


def test_maintenance_triggers_room_unavailable_and_self_heals():
    """Take Lab-CS-2 offline for maintenance. Its bookings must be flagged as
    room_unavailable conflicts and the agents must relocate them."""
    room = repo.query_one("SELECT * FROM rooms WHERE name = 'Lab-CS-2'")
    client = TestClient(app)
    response = client.post(f"/api/rooms/{room['id']}/maintenance",
                           json={"status": "maintenance"})
    assert response.status_code == 200
    assert response.json()["status"] == "maintenance"

    summary = asyncio.run(get_agent("schedule").run_scan())
    unavailable = [c for c in summary["conflicts"] if c["type"] == "room_unavailable"]
    assert unavailable, "maintenance must produce room_unavailable conflicts"

    # handle the Wednesday CS340 session: it must move to the other computer lab
    wednesday = next(b for b in repo.all_bookings()
                     if b["code"] == "CS340" and b["day"] == "Wed"
                     and b["room_name"] == "Lab-CS-2")
    conflict = next(c for c in unavailable if c["booking_a_id"] == wednesday["id"])
    result = asyncio.run(get_agent("coordinator").handle_conflict(conflict["id"]))
    assert result["decision"] == "auto_applied"
    moved = repo.get_booking_full(wednesday["id"])
    assert moved["room_name"] == "Lab-CS-1"
    assert moved["moved_from_name"] == "Lab-CS-2"

    # bring the room back online for subsequent state
    client.post(f"/api/rooms/{room['id']}/maintenance", json={"status": "active"})


def test_agent_status_reports_activity_heartbeat():
    client = TestClient(app)
    data = client.get("/api/agents/status").json()
    names = {a["name"] for a in data["agents"]}
    assert names == {"schedule", "availability", "coordinator",
                     "notifications", "assistant", "analytics"}
    coordinator = next(a for a in data["agents"] if a["name"] == "coordinator")
    assert coordinator["role"]
    assert coordinator["messages_sent"] >= 2, "coordinator must have used the bus"
    assert coordinator["events"] >= 2, "coordinator must have logged events"
    assert "enabled" in data["llm"]
