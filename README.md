# Agent-comms — Smart Campus Multi-Agent System

![CI](https://github.com/Youssefamenallah23/Agent-comms/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-9%2F9-brightgreen)

Autonomous agents that monitor a university timetable, detect conflicts,
negotiate reallocation of rooms and resources, notify affected people and
report on campus utilization — **100% free stack: Python + LangGraph + SQLite
+ FastAPI + free LLM tiers.**

## ✨ What it demonstrates

- **Multi-agent communication** — every agent-to-agent exchange goes through a
  logged message bus (request/response protocol stored in SQLite, visible live
  on the dashboard).
- **Negotiation** — the Coordinator runs a real multi-round loop: gather
  candidates → propose → evaluate → decide → *relax constraints and retry*
  (drop building preference → allow time shifts) → notify.
- **Autonomous decision-making with guardrails** — weighted scoring is
  deterministic and explainable; the LLM only *reasons* about pre-scored
  options and can never invent an invalid move. Instructor availability is
  validated both when gathering candidates and again before applying a move.
- **Human-in-the-loop** — proposals below the auto-apply threshold (or involving
  exams) wait for a human Approve/Reject on the dashboard.
- **Self-adaptation** — the system keeps scanning in the background and
  self-heals new conflicts as they appear (injected or real, e.g. a room going
  offline for maintenance).

## 🤖 The agents

| Agent | LangGraph pipeline | Responsibility |
|---|---|---|
| **Schedule** | `scan → detect → analyze → publish` | Detects 5 conflict types: double booking, instructor clash, capacity overflow, equipment mismatch, room unavailable |
| **Availability** | `parse → query → rank` | Finds free rooms matching hard constraints (capacity, equipment, instructor free) and ranks them |
| **Coordinator** | `intake → gather ⇄ propose → evaluate → decide → notify` | Negotiates, applies policy (exam > core > lab > elective > event), auto-applies ≥ threshold, escalates otherwise |
| **Notifications** | service agent | Queues and delivers alerts to students / instructors / admins |
| **Assistant** | chat | Student Q&A: free rooms, course schedule, room changes, utilization |
| **Analytics** | report | Room utilization, conflict stats, optimization recommendations |

## 🧩 Architecture

```
                       ┌──────────────────────────────────────────────┐
                       │             FastAPI (REST + dashboard)       │
                       └───────┬───────────────┬──────────────┬───────┘
                               │               │              │
                        ScheduleAgent   CoordinatorAgent   AssistantAgent
                        (LangGraph)     (LangGraph)        (LLM/rule)
                          │   ▲               │
                          │   │  message bus  │  (logged request/response)
                          ▼   │               ▼
                       conflicts        AvailabilityAgent ──► ranked candidates
                          │                                     │
                          ▼                                     ▼
                  NotificationAgent ◄──── decision ──── scoring (0..1, explainable)
                          │
                          ▼
                  ┌─────────────── SQLite blackboard ───────────────┐
                  │ rooms · courses · bookings · conflicts ·        │
                  │ proposals · messages · notifications · events   │
                  └─────────────────────────────────────────────────┘
```

## 🚀 Quickstart

```bash
pip install -r requirements.txt
copy .env.example .env        # optional: add free API keys
python run.py                 # start server + dashboard on http://127.0.0.1:8000
python demo.py                # narrative CLI demo (no server needed)
pytest                        # run the test suite (9 tests)
```

The system works **with zero API keys** (rule-based fallback mode). To enable
LLM reasoning, put one of these free keys in `.env`:

| Provider | Free tier | Key URL |
|---|---|---|
| Google Gemini (recommended) | generous per-day free tier, no card | https://aistudio.google.com/apikey |
| Groq | 30 req/min · 1,000 req/day · 200K tok/day | https://console.groq.com/keys |
| OpenRouter | 50 req/day (`:free` models) | https://openrouter.ai/keys |

The provider chain tries them in order (`LLM_PROVIDER_ORDER=gemini,groq,openrouter`),
falls back automatically on rate limits, caches identical prompts, and every
LLM feature has a rule-based fallback.

## 🎮 Demo script (the one to show)

1. Open the dashboard → the seeded campus has **one live conflict**: CS101 and
   MA201 are both booked in Auditorium-A on Wed 10:00–12:00.
2. Click **▶ Run Scan & Auto-Resolve** — watch the negotiation trace:
   Schedule detects the conflict → Coordinator asks Availability for candidates
   → proposals are scored → MA201 moves to C-105 (score 0.925) → notifications
   go out to students and the instructor.
3. Use **⚡ Inject conflict** (double booking / capacity overflow / equipment
   mismatch) to create new chaos and watch the agents self-heal it.
4. Low-score proposals land in **Awaiting approval** — use ✔/✖ to decide as
   the human operator.
5. `POST /api/rooms/{id}/maintenance` takes a room offline — watch the agents
   evacuate its bookings automatically.

## 🔌 API

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/state` | Everything the dashboard shows |
| GET | `/api/agents/status` | Per-agent heartbeat: events + bus traffic |
| POST | `/api/pipeline/scan-and-resolve` | One-click detect + negotiate + apply |
| POST | `/api/simulate/seed` · `/api/simulate/inject` | Demo data & chaos monkey |
| POST | `/api/rooms/{id}/maintenance` | Take a room offline / bring it back |
| POST | `/api/agents/schedule/scan` | Schedule Agent graph run |
| POST | `/api/agents/availability/query` | Availability Agent graph run |
| POST | `/api/agents/coordinator/handle/{id}` | Negotiate one conflict |
| POST | `/api/proposals/{id}/approve` · `/reject` | Human-in-the-loop |
| POST | `/api/agents/assistant/chat` | Student chatbot |
| GET | `/api/agents/analytics/report` | Utilization & recommendations |
| GET | `/api/notifications` | Public notifications feed (Student Desk) |
| GET | `/api/agents/assistant/history` | Restore a chat session after reload |
| POST | `/api/bookings` | Book a room; rejected requests get **ranked alternatives** |
| POST | `/api/bookings/{id}/cancel` | Cancel a booking, campus re-scans itself |
| POST | `/api/requests` | Student room request: auto-confirm or **time-shifted proposals** |
| GET | `/api/agents/analytics/report.csv` | Utilization report as CSV |
| GET | `/api/conflicts/{id}` · `/api/messages` | Full audit trail |

## 🧠 Agentic behaviours added in v1.1

- **Campus life is interactive** — you can create bookings (with hard-constraint
  validation and agent-ranked alternatives on rejection), cancel them, and make
  student room requests that auto-confirm strong fits or propose time shifts.
- **The agents learn** — every accepted reallocation records the instructor's
  building; future negotiations prefer buildings the instructor was moved into
  before (a tiny preference memory, grown from its own decisions).
- **Self-monitoring** — every mutation triggers a fresh conflict scan, so the
  campus re-checks itself after each change.
- **Concurrency-safe storage** — SQLite busy-timeout plus bounded retries; the
  LLM cache is bounded so long sessions stay predictable.

## 🎓 Student Desk (`/student`)

A student-facing portal served at **`/student`** (designed with the Hallmark
design system):

- **Ask the campus** — chat with the Assistant Agent: free rooms, course
  schedules, room changes, utilization. Suggestion chips for common questions,
  conversation restored on reload via the history endpoint, and a visible
  "answered via" tag showing which intent served the answer (rule-based or LLM).
- **Notifications inbox** — the same alerts the Notification Agent dispatches
  (room changes, approvals, escalations), filtered by audience: students,
  instructors or admin, refreshing live every few seconds.

The Assistant is strictly read-only: it can inform, never mutate the campus.

## ⚙️ Policy knobs (`.env`)

- `AUTO_APPLY_THRESHOLD=0.75` — score needed to apply without a human
- `MAX_NEGOTIATION_ROUNDS=3` — negotiation retries with relaxed constraints
- `SCAN_INTERVAL_SEC=60` — background monitoring cadence
- `AGENT_TRANSPORT=inline|http` — in-process handlers or real REST A2A calls

## 🗺 Roadmap ideas

- WebSocket live trace instead of polling
- Preference learning (instructor room/time preferences)
- Exam timetabling solver (constraint programming alongside agents)
- Multi-campus federation with a second Coordinator tier
- Email/Telegram notification channels
