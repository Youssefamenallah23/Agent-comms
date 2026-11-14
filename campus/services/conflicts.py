"""Deterministic multi-type conflict detection (the Schedule Agent's engine)."""
import json

from ..db import repo

PRIORITY_RANK = {"exam": 0, "core": 1, "lab": 2, "elective": 3, "event": 4}


def _severity(kind: str, a: dict, b: dict | None = None) -> str:
    if kind == "instructor_clash":
        return "high"
    if kind == "room_unavailable":
        return "high"
    if kind == "double_booking":
        ranks = [PRIORITY_RANK.get(a["priority"], 3)]
        if b:
            ranks.append(PRIORITY_RANK.get(b["priority"], 3))
        return "high" if min(ranks) <= 1 else "medium"
    if kind == "capacity_overflow":
        ratio = a["enrolled"] / max(1, a["capacity"])
        return "high" if ratio > 1.15 else "medium"
    return "medium"  # equipment_mismatch


def _describe(kind: str, a: dict, b: dict | None = None, missing=None) -> str:
    slot = f"{a['day']} {a['start_time']}-{a['end_time']}"
    if kind == "double_booking":
        return (f"Double booking: {a['code']} ({a['enrolled']} students) and "
                f"{b['code']} ({b['enrolled']} students) are both assigned to "
                f"{a['room_name']} on {slot}.")
    if kind == "instructor_clash":
        return (f"Instructor clash: {a['instructor']} teaches {a['code']} in {a['room_name']} "
                f"and {b['code']} in {b['room_name']} on {slot}.")
    if kind == "capacity_overflow":
        return (f"Capacity overflow: {a['code']} has {a['enrolled']} enrolled but "
                f"{a['room_name']} only seats {a['capacity']} ({slot}).")
    if kind == "equipment_mismatch":
        return (f"Equipment mismatch: {a['code']} requires {', '.join(missing or [])} "
                f"which {a['room_name']} lacks ({slot}).")
    if kind == "room_unavailable":
        return (f"Room unavailable: {a['code']} is scheduled in {a['room_name']} "
                f"which is under maintenance ({slot}).")
    return f"Scheduling issue for {a['code']} ({slot})."


def _mk(kind, a, b=None, missing=None) -> dict:
    return {
        "type": kind,
        "booking_a_id": a["id"],
        "booking_b_id": b["id"] if b else None,
        "room_id": a["room_id"],
        "description": _describe(kind, a, b, missing),
        "severity": _severity(kind, a, b),
        "day": a["day"],
        "start": a["start_time"],
        "end": a["end_time"],
        "courses": sorted({a["code"], *( [b["code"]] if b else [] )}),
    }


def detect_all() -> list[dict]:
    """Scan all confirmed bookings, persist NEW conflicts (deduped) and return them."""
    bookings = repo.all_bookings()
    found: list[dict] = []

    # 1) double bookings: same room, same day, overlapping times
    by_room_day: dict[tuple, list] = {}
    for bk in bookings:
        by_room_day.setdefault((bk["room_id"], bk["day"]), []).append(bk)
    for bs in by_room_day.values():
        for i in range(len(bs)):
            for j in range(i + 1, len(bs)):
                a, c = bs[i], bs[j]
                if repo.overlaps(a["start_time"], a["end_time"], c["start_time"], c["end_time"]):
                    first, second = sorted([a, c], key=lambda x: (x["start_time"], x["id"]))
                    found.append(_mk("double_booking", first, second))

    # 2) instructor clashes: same instructor, overlapping, different rooms
    by_ins_day: dict[tuple, list] = {}
    for bk in bookings:
        by_ins_day.setdefault((bk["instructor"], bk["day"]), []).append(bk)
    for bs in by_ins_day.values():
        for i in range(len(bs)):
            for j in range(i + 1, len(bs)):
                a, c = bs[i], bs[j]
                if a["room_id"] != c["room_id"] and repo.overlaps(
                    a["start_time"], a["end_time"], c["start_time"], c["end_time"]
                ):
                    first, second = sorted([a, c], key=lambda x: (x["start_time"], x["id"]))
                    found.append(_mk("instructor_clash", first, second))

    # 3) per-booking issues
    for bk in bookings:
        if bk["room_status"] == "maintenance":
            found.append(_mk("room_unavailable", bk))
            continue
        if bk["enrolled"] > bk["capacity"]:
            found.append(_mk("capacity_overflow", bk))
        required = bk["required_equipment"] or []
        have = bk["equipment"] or []
        missing = [e for e in required if e not in have]
        if missing:
            found.append(_mk("equipment_mismatch", bk, missing=missing))

    # persist with de-duplication (same type + same bookings still open)
    created = []
    for conflict in found:
        if repo.find_open_conflict(conflict["type"], conflict["booking_a_id"], conflict["booking_b_id"]):
            continue
        conflict_id = repo.insert_conflict(
            conflict["type"], conflict["booking_a_id"], conflict["booking_b_id"],
            conflict["room_id"], conflict["description"], conflict["severity"],
        )
        conflict["id"] = conflict_id
        created.append(conflict)
    return created
