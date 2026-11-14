"""Process-wide singletons (agents, LLM, bus) shared by the API and demo scripts."""
from .config import settings
from .llm import build_llm
from .bus import bus

_agents: dict = {}
_llm = None


def setup() -> None:
    global _llm
    if _agents:
        return
    _llm = build_llm(settings)

    from .agents.schedule_agent import ScheduleAgent
    from .agents.availability_agent import AvailabilityAgent
    from .agents.coordinator_agent import CoordinatorAgent
    from .agents.notification_agent import NotificationAgent
    from .agents.assistant_agent import AssistantAgent
    from .agents.analytics_agent import AnalyticsAgent

    schedule = ScheduleAgent(_llm, bus)
    availability = AvailabilityAgent(_llm, bus)
    coordinator = CoordinatorAgent(_llm, bus)
    notifications = NotificationAgent(_llm, bus)
    assistant = AssistantAgent(_llm, bus)
    analytics = AnalyticsAgent(_llm, bus)

    _agents.update({
        "schedule": schedule,
        "availability": availability,
        "coordinator": coordinator,
        "notifications": notifications,
        "assistant": assistant,
        "analytics": analytics,
    })

    bus.register("schedule", "scan", schedule.run_scan)
    bus.register("availability", "query", availability.run_query)
    bus.register("coordinator", "handle", coordinator.handle_conflict_payload)
    bus.register("notifications", "dispatch", notifications.dispatch)
    bus.register("notifications", "flush", lambda payload: notifications.flush())
    bus.register("assistant", "chat", assistant.chat_payload)
    bus.register("analytics", "report", analytics.report_payload)


def get_agent(name: str):
    setup()
    return _agents[name]


def get_handler(agent: str, action: str):
    setup()
    return bus.get_handler(agent, action)


def llm_status() -> dict:
    setup()
    return {
        "enabled": _llm.available() if _llm else False,
        "providers": _llm.provider_names() if _llm else [],
        "calls": _llm.calls if _llm else 0,
    }
