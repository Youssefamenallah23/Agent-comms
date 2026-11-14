# Agent-comms — Smart Campus Multi-Agent System

Autonomous agents that monitor a university timetable, detect conflicts,
negotiate reallocation of rooms and resources, notify affected people and
report on campus utilization.

## Vision

Instead of one monolithic scheduling app, the campus runs a **society of
cooperating agents**:

| Agent | Role |
|---|---|
| **Schedule Agent** | scans the timetable, detects conflicts (double booking, instructor clash, capacity overflow, equipment mismatch, room unavailable) |
| **Availability Agent** | finds and ranks free rooms for a request |
| **Coordinator Agent** | negotiates multi-round reallocations and applies the best one |
| **Notification Agent** | informs students, instructors and admins |
| **Assistant Agent** | answers student questions (free rooms, schedules, changes) |
| **Analytics Agent** | reports utilization and recommends optimizations |

Agents talk through a **logged message bus**, keep a **shared SQLite
blackboard**, and are orchestrated with **LangGraph** state machines.

## Stack (100% free)

- Python + LangGraph (agent orchestration)
- FastAPI (API + dashboard backend)
- SQLite (shared memory / blackboard)
- Free LLM tiers with automatic fallback: Gemini → Groq → OpenRouter
  (the system also runs fully rule-based with zero API keys)

## Status

🚧 Under active development — see the commit history for the step-by-step build.
