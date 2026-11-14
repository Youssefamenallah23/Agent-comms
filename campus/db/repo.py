"""SQLite data layer - the shared "blackboard" every agent reads and writes."""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from ..config import settings

_SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sql")


# ---------------------------------------------------------------- core utils
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with closing(connect()) as conn:
        with open(_SCHEMA, encoding="utf-8") as fh:
            conn.executescript(fh.read())
        conn.execute("PRAGMA journal_mode = WAL")
        conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def query(sql: str, params=()) -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def query_one(sql: str, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=()):
    with closing(connect()) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def is_empty() -> bool:
    return (query_one("SELECT COUNT(*) AS c FROM rooms") or {"c": 0})["c"] == 0


def clear_all_data() -> None:
    for table in ("assistant_chats", "notifications", "messages", "proposals",
                  "conflicts", "bookings", "courses", "rooms", "events_log"):
        execute(f"DELETE FROM {table}")


# ---------------------------------------------------------------- time utils
def minutes(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def overlaps(s1: str, e1: str, s2: str, e2: str) -> bool:
    return minutes(s1) < minutes(e2) and minutes(s2) < minutes(e1)


def add_minutes(t: str, delta: int) -> str:
    total = max(0, min(24 * 60, minutes(t) + delta))
    return f"{total // 60:02d}:{total % 60:02d}"


def _json(text, default):
    if text is None:
        return default
    if isinstance(text, (list, dict)):
        return text
    try:
        return json.loads(text)
    except Exception:
        return default


# ---------------------------------------------------------------- rooms
def insert_room(name, building, capacity, equipment, status="active") -> int:
    return execute(
        "INSERT INTO rooms (name, building, capacity, equipment, status) VALUES (?,?,?,?,?)",
        (name, building, capacity, json.dumps(equipment), status),
    )


def all_rooms(active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM rooms"
    if active_only:
        sql += " WHERE status = 'active'"
    rows = query(sql + " ORDER BY id")
    for r in rows:
        r["equipment"] = _json(r["equipment"], [])
    return rows


def get_room(room_id: int):
    row = query_one("SELECT * FROM rooms WHERE id = ?", (room_id,))
    if row:
        row["equipment"] = _json(row["equipment"], [])
    return row


def set_room_status(room_id: int, status: str) -> None:
    execute("UPDATE rooms SET status = ? WHERE id = ?", (status, room_id))


# ---------------------------------------------------------------- courses
def insert_course(code, title, instructor, enrolled, priority, required_equipment) -> int:
    return execute(
        "INSERT INTO courses (code, title, instructor, enrolled, priority, required_equipment)"
        " VALUES (?,?,?,?,?,?)",
        (code, title, instructor, enrolled, priority, json.dumps(required_equipment)),
    )


def all_courses() -> list[dict]:
    rows = query("SELECT * FROM courses ORDER BY id")
    for c in rows:
        c["required_equipment"] = _json(c["required_equipment"], [])
    return rows


def get_course_by_code(code: str):
    row = query_one("SELECT * FROM courses WHERE code = ?", (code,))
    if row:
        row["required_equipment"] = _json(row["required_equipment"], [])
    return row


# ---------------------------------------------------------------- bookings
_BOOKING_SELECT = """
SELECT b.*, c.code, c.title, c.instructor, c.enrolled, c.priority, c.required_equipment,
       r.name AS room_name, r.building, r.capacity, r.equipment, r.status AS room_status,
       rm.name AS moved_from_name
FROM bookings b
JOIN courses c ON c.id = b.course_id
JOIN rooms r  ON r.id = b.room_id
LEFT JOIN rooms rm ON rm.id = b.moved_from
"""


def _decorate(row: dict) -> dict:
    row["required_equipment"] = _json(row["required_equipment"], [])
    row["equipment"] = _json(row["equipment"], [])
    return row


def insert_booking(course_id, room_id, day, start_time, end_time, created_by="system") -> int:
    return execute(
        "INSERT INTO bookings (course_id, room_id, day, start_time, end_time, created_by)"
        " VALUES (?,?,?,?,?,?)",
        (course_id, room_id, day, start_time, end_time, created_by),
    )


def all_bookings() -> list[dict]:
    rows = query(
        _BOOKING_SELECT + " WHERE b.status != 'cancelled' ORDER BY CASE b.day"
        " WHEN 'Mon' THEN 1 WHEN 'Tue' THEN 2 WHEN 'Wed' THEN 3 WHEN 'Thu' THEN 4"
        " ELSE 5 END, b.start_time"
    )
    return [_decorate(r) for r in rows]


def get_booking_full(booking_id: int):
    row = query_one(_BOOKING_SELECT + " WHERE b.id = ?", (booking_id,))
    return _decorate(row) if row else None


def update_booking_room(booking_id, new_room_id, new_day=None, new_start=None, new_end=None):
    execute(
        "UPDATE bookings SET room_id = ?, moved_from = COALESCE(moved_from, room_id),"
        " status = 'moved', day = COALESCE(?, day), start_time = COALESCE(?, start_time),"
        " end_time = COALESCE(?, end_time) WHERE id = ?",
        (new_room_id, new_day, new_start, new_end, booking_id),
    )


def cancel_booking(booking_id: int) -> None:
    execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))


def room_free(room_id: int, day: str, start: str, end: str, exclude_booking_ids=()) -> bool:
    room = get_room(room_id)
    if not room or room["status"] != "active":
        return False
    rows = query(
        "SELECT id, start_time, end_time FROM bookings"
        " WHERE room_id = ? AND day = ? AND status != 'cancelled'",
        (room_id, day),
    )
    for row in rows:
        if row["id"] in exclude_booking_ids:
            continue
        if overlaps(row["start_time"], row["end_time"], start, end):
            return False
    return True


def rooms_free(day: str, start: str, end: str, exclude_room_ids=()) -> list[dict]:
    rooms = all_rooms(active_only=True)
    busy = query(
        "SELECT room_id, start_time, end_time FROM bookings"
        " WHERE day = ? AND status != 'cancelled'",
        (day,),
    )
    free_ids = {r["id"] for r in rooms} - set(exclude_room_ids)
    for b in busy:
        if b["room_id"] in free_ids and overlaps(b["start_time"], b["end_time"], start, end):
            free_ids.discard(b["room_id"])
    return [r for r in rooms if r["id"] in free_ids]


def room_utilization() -> list[dict]:
    rooms = all_rooms()
    rows = query("SELECT room_id, start_time, end_time FROM bookings WHERE status != 'cancelled'")
    hours: dict[int, float] = {}
    for row in rows:
        hours[row["room_id"]] = hours.get(row["room_id"], 0.0) + (
            minutes(row["end_time"]) - minutes(row["start_time"])
        ) / 60.0
    weekly_capacity = 5 * 8.0  # Mon-Fri on the 08:00-18:00 grid
    out = []
    for room in rooms:
        used = hours.get(room["id"], 0.0)
        out.append({
            **room,
            "booked_hours": round(used, 1),
            "utilization": round(100.0 * used / weekly_capacity, 1),
        })
    return out


# ---------------------------------------------------------------- conflicts
def insert_conflict(kind, booking_a_id, booking_b_id, room_id, description, severity) -> int:
    return execute(
        "INSERT INTO conflicts (type, booking_a_id, booking_b_id, room_id, description,"
        " severity, status, detected_at) VALUES (?,?,?,?,?,?, 'open', ?)",
        (kind, booking_a_id, booking_b_id, room_id, description, severity, now_iso()),
    )


def find_open_conflict(kind, booking_a_id, booking_b_id):
    return query_one(
        "SELECT * FROM conflicts WHERE type = ? AND booking_a_id = ?"
        " AND booking_b_id IS ? AND status IN ('open', 'pending_approval')",
        (kind, booking_a_id, booking_b_id),
    )


def get_conflict(conflict_id: int):
    return query_one("SELECT * FROM conflicts WHERE id = ?", (conflict_id,))


def open_conflicts() -> list[dict]:
    return query("SELECT * FROM conflicts WHERE status = 'open' ORDER BY id DESC")


def all_conflicts(limit: int = 50) -> list[dict]:
    return query("SELECT * FROM conflicts ORDER BY id DESC LIMIT ?", (limit,))


def set_conflict_status(conflict_id: int, status: str) -> None:
    resolved_at = now_iso() if status == "resolved" else None
    execute(
        "UPDATE conflicts SET status = ?, resolved_at = COALESCE(?, resolved_at) WHERE id = ?",
        (status, resolved_at, conflict_id),
    )


def set_conflict_analysis(conflict_id: int, analysis: str) -> None:
    execute("UPDATE conflicts SET llm_analysis = ? WHERE id = ?", (analysis, conflict_id))


# ---------------------------------------------------------------- proposals
def insert_proposal(conflict_id, round_no, action, booking_id, new_room_id,
                    new_day, new_start, new_end, score, breakdown, reasoning, status) -> int:
    return execute(
        "INSERT INTO proposals (conflict_id, round, action, booking_id, new_room_id,"
        " new_day, new_start, new_end, score, breakdown, reasoning, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (conflict_id, round_no, action, booking_id, new_room_id, new_day, new_start,
         new_end, score, breakdown, reasoning, status, now_iso()),
    )


def get_proposal(proposal_id: int):
    return query_one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))


def proposals_for(conflict_id: int) -> list[dict]:
    return query("SELECT * FROM proposals WHERE conflict_id = ? ORDER BY score DESC, id", (conflict_id,))


def set_proposal_status(proposal_id: int, status: str) -> None:
    execute("UPDATE proposals SET status = ? WHERE id = ?", (status, proposal_id))


def update_proposal_score(proposal_id: int, score: float, breakdown: str) -> None:
    execute("UPDATE proposals SET score = ?, breakdown = ? WHERE id = ?",
            (score, breakdown, proposal_id))


def update_proposal_reasoning(proposal_id: int, reasoning: str) -> None:
    execute("UPDATE proposals SET reasoning = ? WHERE id = ?", (reasoning, proposal_id))


# ---------------------------------------------------------------- messages (A2A bus trace)
def insert_message(correlation_id, from_agent, to_agent, role, topic, content: dict) -> None:
    execute(
        "INSERT INTO messages (correlation_id, from_agent, to_agent, role, topic, content, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (correlation_id, from_agent, to_agent, role, topic, json.dumps(content, default=str), now_iso()),
    )


def recent_messages(limit: int = 50) -> list[dict]:
    rows = query("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["content"] = _json(r["content"], {})
    return list(reversed(rows))


def messages_like(pattern: str, limit: int = 100) -> list[dict]:
    rows = query(
        "SELECT * FROM messages WHERE correlation_id LIKE ? ORDER BY id LIMIT ?",
        (pattern, limit),
    )
    for r in rows:
        r["content"] = _json(r["content"], {})
    return rows


# ---------------------------------------------------------------- notifications
def insert_notification(audience, subject, body, conflict_id=None) -> int:
    return execute(
        "INSERT INTO notifications (audience, subject, body, status, conflict_id, created_at)"
        " VALUES (?,?,?, 'pending', ?, ?)",
        (audience, subject, body, conflict_id, now_iso()),
    )


def pending_notifications() -> list[dict]:
    return query("SELECT * FROM notifications WHERE status = 'pending' ORDER BY id")


def mark_notification_sent(notification_id: int) -> None:
    execute("UPDATE notifications SET status = 'sent', sent_at = ? WHERE id = ?",
            (now_iso(), notification_id))


def recent_notifications(limit: int = 20) -> list[dict]:
    return query("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------- events / chats
def insert_event(agent, event_type, payload: dict) -> None:
    execute(
        "INSERT INTO events_log (agent, event_type, payload, created_at) VALUES (?,?,?,?)",
        (agent, event_type, json.dumps(payload, default=str), now_iso()),
    )


def recent_events(limit: int = 50) -> list[dict]:
    rows = query("SELECT * FROM events_log ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["payload"] = _json(r["payload"], {})
    return list(reversed(rows))


def insert_assistant_chat(session_id, role, content) -> None:
    execute(
        "INSERT INTO assistant_chats (session_id, role, content, created_at) VALUES (?,?,?,?)",
        (session_id, role, content, now_iso()),
    )


def assistant_history(session_id: str, limit: int = 50) -> list[dict]:
    rows = query(
        "SELECT role, content, created_at FROM assistant_chats"
        " WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    )
    return list(reversed(rows))


def conflict_stats() -> dict:
    by_status = {r["status"]: r["c"] for r in query(
        "SELECT status, COUNT(*) c FROM conflicts GROUP BY status")}
    by_type = {r["type"]: r["c"] for r in query(
        "SELECT type, COUNT(*) c FROM conflicts GROUP BY type")}
    return {"by_status": by_status, "by_type": by_type}

