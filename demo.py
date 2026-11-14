"""Narrative end-to-end demo: seed -> detect -> negotiate -> resolve -> notify.

Run:  python demo.py
Works with or without API keys (rule-based mode when no keys are configured).
"""
import asyncio
import os
import sys
import tempfile

os.environ["CAMPUS_DB"] = os.path.join(tempfile.gettempdir(), "campus_demo.db")
os.environ.setdefault("BACKGROUND_TASKS", "0")
os.environ.setdefault("SEED_ON_START", "0")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    os.remove(os.environ["CAMPUS_DB"])
except FileNotFoundError:
    pass

from campus.db import repo                      # noqa: E402
from campus.simulation import seed              # noqa: E402
from campus.runtime import get_agent, setup     # noqa: E402


def line(title: str) -> None:
    print("\n" + "=" * 72 + f"\n  {title}\n" + "=" * 72)


async def main() -> None:
    setup()
    repo.init_db()
    seed.seed_data(force=True)

    line("1) CAMPUS SEEDED")
    print(f"   rooms: {len(repo.all_rooms())}  courses: {len(repo.all_courses())}  "
          f"bookings: {len(repo.all_bookings())}")
    llm = get_agent("coordinator").llm
    print(f"   LLM mode: {', '.join(llm.provider_names()) if llm.available() else 'rule-based (no API keys set)'}")

    line("2) SCHEDULE AGENT - scanning timetable")
    summary = await get_agent("schedule").run_scan()
    print(f"   scanned {summary['scanned_bookings']} bookings -> "
          f"{summary['new_conflicts']} new conflict(s)")
    for conflict in summary["conflicts"]:
        print(f"   [CONFLICT #{conflict['id']}] {conflict['severity'].upper()} "
              f"{conflict['type']}")
        print(f"      {conflict['description']}")

    line("3) COORDINATOR AGENT - negotiating reallocations")
    for conflict in summary["conflicts"]:
        result = await get_agent("coordinator").handle_conflict(conflict["id"])
        best = result.get("best") or {}
        slot = best.get("slot") or {}
        print(f"   conflict #{result['conflict_id']}: decision = {result['decision']}")
        if best:
            print(f"      best proposal: move to {best.get('room_name')} "
                  f"({slot.get('day')} {slot.get('start')}-{slot.get('end')}) "
                  f"score={best.get('score')}")
            print(f"      reasoning: {best.get('reasoning')}")

    first_id = summary["conflicts"][0]["id"] if summary["conflicts"] else None
    if first_id:
        line("4) AGENT-TO-AGENT MESSAGE TRACE (negotiation log)")
        for m in repo.messages_like(f"cf-{first_id}-%", 60):
            print(f"   {m['from_agent']:<12} -> {m['to_agent']:<12} [{m['role']:>9}] {m['topic']}")

    line("5) NOTIFICATIONS DISPATCHED")
    for n in repo.recent_notifications(10):
        print(f"   [{n['status']}] {n['audience']}: {n['subject']}")

    line("6) FINAL STATE + ANALYTICS")
    for b in repo.all_bookings():
        if b["status"] == "moved":
            print(f"   moved: {b['code']} {b['moved_from_name']} -> {b['room_name']} "
                  f"({b['day']} {b['start_time']}-{b['end_time']})")
    report = await get_agent("analytics").report()
    print(f"   {report['summary']}")
    line("DEMO COMPLETE - run the dashboard with: python run.py  ->  http://127.0.0.1:8000")


if __name__ == "__main__":
    asyncio.run(main())
