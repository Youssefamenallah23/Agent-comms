"""Notification Agent - delivers alerts to students, instructors and admins."""
import logging

from .. import repo
from .base import BaseAgent

log = logging.getLogger("notifications")


class NotificationAgent(BaseAgent):
    name = "notifications"

    async def dispatch(self, payload: dict) -> dict:
        """Queue notification items then flush them (in-app channel simulation)."""
        conflict_id = payload.get("conflict_id")
        queued = 0
        for item in payload.get("items", []):
            repo.insert_notification(
                item.get("audience", "students"),
                item.get("subject", ""),
                item.get("body", ""),
                conflict_id,
            )
            queued += 1
        sent = self.flush()
        self.log_event("dispatch", {"queued": queued, "sent": sent})
        return {"queued": queued, "sent": sent}

    def flush(self) -> int:
        pending = repo.pending_notifications()
        for notification in pending:
            repo.mark_notification_sent(notification["id"])
        if pending:
            self.log_event("flushed", {"count": len(pending)})
        return len(pending)

    def run(self) -> int:
        return self.flush()
