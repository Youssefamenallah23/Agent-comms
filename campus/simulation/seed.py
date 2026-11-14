"""Demo campus: seed data generator + conflict injector for live demos."""
import json
import random

from .. import repo

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
SLOTS = [("08:00", "10:00"), ("10:00", "12:00"), ("14:00", "16:00"), ("16:00", "18:00")]

ROOMS = [
    # name, building, capacity, equipment
    ("Auditorium-A", "A", 150, ["projector", "microphone"]),
    ("B-201", "B", 40, ["projector"]),
    ("B-202", "B", 40, ["projector", "whiteboard"]),
    ("C-105", "C", 50, ["projector"]),
    ("Lab-CS-1", "B", 30, ["computers", "projector"]),
    ("Lab-CS-2", "B", 30, ["computers"]),
    ("Seminar-Hall", "A", 25, ["whiteboard"]),
]

COURSES = [
    # code, title, instructor, enrolled, priority, required_equipment
    ("CS101", "Intro to Programming", "Dr. Chen", 60, "core", ["projector"]),
    ("MA201", "Linear Algebra", "Dr. Okafor", 45, "core", ["projector"]),
    ("PH101", "Physics I", "Dr. Rossi", 50, "core", ["projector"]),
    ("CS340", "Databases Lab", "Dr. Chen", 24, "lab", ["computers"]),
    ("BI210", "Bio-Statistics Lab", "Dr. Singh", 28, "lab", ["computers"]),
    ("CHEM110", "Chemistry Lab", "Dr. Rossi", 26, "lab", ["computers"]),
    ("HIST105", "World History", "Dr. Laurent", 35, "elective", []),
    ("ART120", "Design Basics", "Dr. Laurent", 20, "elective", ["whiteboard"]),
    ("ECON202", "Microeconomics", "Dr. Okafor", 55, "core", ["projector"]),
    ("MATH490", "Topology Seminar", "Dr. Okafor", 15, "elective", ["whiteboard"]),
    ("MID-PH", "Physics Midterm", "Dr. Rossi", 50, "exam", ["projector"]),
]

BOOKINGS = [
    # course, room, day, start, end
    ("CS101", "Auditorium-A", "Mon", "08:00", "10:00"),
    ("PH101", "C-105", "Mon", "08:00", "10:00"),
    ("MA201", "C-105", "Mon", "10:00", "12:00"),
    ("CS340", "Lab-CS-1", "Mon", "10:00", "12:00"),
    ("HIST105", "B-202", "Mon", "14:00", "16:00"),
    ("BI210", "Lab-CS-2", "Mon", "14:00", "16:00"),
    ("MA201", "C-105", "Tue", "10:00", "12:00"),
    ("PH101", "Auditorium-A", "Tue", "10:00", "12:00"),
    ("ECON202", "Auditorium-A", "Tue", "14:00", "16:00"),
    ("CHEM110", "Lab-CS-1", "Tue", "14:00", "16:00"),
    ("ART120", "B-202", "Tue", "14:00", "16:00"),
    ("CS340", "Lab-CS-2", "Wed", "08:00", "10:00"),
    ("CS101", "Auditorium-A", "Wed", "10:00", "12:00"),
    ("MA201", "Auditorium-A", "Wed", "10:00", "12:00"),  # seeded conflict (double booking)
    ("MATH490", "B-202", "Wed", "14:00", "16:00"),
    ("HIST105", "C-105", "Wed", "14:00", "16:00"),
    ("CS101", "Auditorium-A", "Thu", "08:00", "10:00"),
    ("PH101", "C-105", "Thu", "10:00", "12:00"),
    ("BI210", "Lab-CS-1", "Thu", "10:00", "12:00"),
    ("MATH490", "B-202", "Thu", "14:00", "16:00"),
    ("ART120", "B-202", "Fri", "10:00", "12:00"),
    ("CS340", "Lab-CS-2", "Fri", "10:00", "12:00"),
    ("MID-PH", "Auditorium-A", "Fri", "14:00", "16:00"),
]


def seed_data(force: bool = False) -> bool:
    """Populate the demo campus. Returns True if data was (re)created."""
    if not force and not repo.is_empty():
        return False
    repo.clear_all_data()
    room_ids = {n: repo.insert_room(n, b, c, e) for n, b, c, e in ROOMS}
    course_ids = {code: repo.insert_course(code, t, i, en, p, eq)
                  for code, t, i, en, p, eq in COURSES}
    for code, room_name, day, start, end in BOOKINGS:
        repo.insert_booking(course_ids[code], room_ids[room_name], day, start, end)
    return True


def _instructor_free(instructor: str, day: str, start: str, end: str) -> bool:
    for b in repo.all_bookings():
        if (b["instructor"] == instructor and b["day"] == day
                and repo.overlaps(b["start_time"], b["end_time"], start, end)):
            return False
    return True


def inject_conflict(kind: str) -> dict:
    """Deliberately create a realistic conflict so agents have something to solve."""
    courses = {c["code"]: c for c in repo.all_courses()}
    rooms = {r["name"]: r for r in repo.all_rooms()}

    if kind == "double_booking":
        bookings = repo.all_bookings()
        random.shuffle(bookings)
        for b in bookings:
            for code, course in courses.items():
                if course["id"] == b["course_id"] or course["instructor"] == b["instructor"]:
                    continue
                clashes = any(
                    x["code"] == code and x["day"] == b["day"]
                    and repo.overlaps(x["start_time"], x["end_time"], b["start_time"], b["end_time"])
                    for x in repo.all_bookings()
                )
                if clashes or not _instructor_free(course["instructor"], b["day"],
                                                   b["start_time"], b["end_time"]):
                    continue
                bid = repo.insert_booking(course["id"], b["room_id"], b["day"],
                                          b["start_time"], b["end_time"], "injector")
                return {"injected": kind, "booking_id": bid,
                        "detail": f"{code} double-booked into {b['room_name']} on "
                                  f"{b['day']} {b['start_time']}-{b['end_time']}"}

    if kind == "capacity_overflow":
        for course in courses.values():
            for room in rooms.values():
                if course["enrolled"] <= room["capacity"]:
                    continue
                for day in DAYS:
                    for start, end in SLOTS:
                        if not repo.room_free(room["id"], day, start, end):
                            continue
                        if not _instructor_free(course["instructor"], day, start, end):
                            continue
                        bid = repo.insert_booking(course["id"], room["id"], day,
                                                  start, end, "injector")
                        return {"injected": kind, "booking_id": bid,
                                "detail": f"{course['code']} ({course['enrolled']} students) "
                                          f"booked into {room['name']} (seats "
                                          f"{room['capacity']}) on {day} {start}-{end}"}

    if kind == "equipment_mismatch":
        for course in courses.values():
            required = course["required_equipment"] or []
            if "computers" not in required:
                continue
            for room in rooms.values():
                if "computers" in room["equipment"]:
                    continue
                for day in DAYS:
                    for start, end in SLOTS:
                        if not repo.room_free(room["id"], day, start, end):
                            continue
                        if not _instructor_free(course["instructor"], day, start, end):
                            continue
                        bid = repo.insert_booking(course["id"], room["id"], day,
                                                  start, end, "injector")
                        return {"injected": kind, "booking_id": bid,
                                "detail": f"{course['code']} needs computers but was "
                                          f"booked into {room['name']} on {day} {start}-{end}"}

    return {"injected": kind, "detail": "no suitable slot found - try another type"}
