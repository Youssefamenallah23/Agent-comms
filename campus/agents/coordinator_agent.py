"""Coordinator Agent - the negotiation hub.

LangGraph pipeline (with a real negotiation loop):

    START -> intake -> gather <-> propose -> evaluate -> decide -> notify -> END
                                     ^______________|
                             (retry with relaxed constraints, up to N rounds)

The Coordinator asks the Availability Agent for candidates over the message
bus, turns them into scored proposals, asks the LLM to negotiate (or falls
back to rules), then auto-applies when policy allows or queues the best
proposal for human approval.
"""
import json
import logging
import operator
import uuid
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from .. import repo
from ..config import settings
from ..services import scoring
from .base import BaseAgent

log = logging.getLogger("coordinator")

PRIORITY_RANK = {"exam": 0, "core": 1, "lab": 2, "elective": 3, "event": 4}


class CoordinatorState(TypedDict, total=False):
    conflict_id: int
    conflict: dict
    bookings: list
    mover_id: int
    candidates: list
    proposals: Annotated[list, operator.add]
    best: dict
    round: int
    decision: str
    correlation_id: str
    moved_booking_id: int
    trace: Annotated[list, operator.add]


def _brief(b: dict) -> dict:
    return {
        "booking_id": b["id"], "course": b["code"], "title": b["title"],
        "instructor": b["instructor"], "enrolled": b["enrolled"], "priority": b["priority"],
        "needs": b["required_equipment"], "room": b["room_name"], "building": b["building"],
        "capacity": b["capacity"], "day": b["day"], "start": b["start_time"], "end": b["end_time"],
    }


def apply_proposal(proposal_id: int) -> dict:
    """Apply a proposal: verify the slot is still free, move the booking,
    resolve the conflict, reject losing proposals. Used by both the
    Coordinator graph (auto-apply) and the human approval endpoint."""
    proposal = repo.get_proposal(proposal_id)
    if not proposal or proposal["status"] in ("applied", "rejected"):
        return {"ok": False, "reason": "proposal missing or already decided"}
    conflict = repo.get_conflict(proposal["conflict_id"])
    involved = [i for i in (conflict["booking_a_id"], conflict["booking_b_id"]) if i]
    free = repo.room_free(
        proposal["new_room_id"], proposal["new_day"], proposal["new_start"],
        proposal["new_end"], exclude_booking_ids=involved,
    )
    if not free:
        repo.set_proposal_status(proposal_id, "rejected")
        repo.set_conflict_status(conflict["id"], "escalated")
        return {"ok": False, "reason": "slot no longer free - conflict escalated"}
    # revalidate instructor availability at the destination slot
    mover = repo.get_booking_full(proposal["booking_id"])
    instructor_busy = any(
        b["instructor"] == mover["instructor"]
        and b["day"] == proposal["new_day"]
        and b["id"] not in involved
        and b["id"] != proposal["booking_id"]
        and repo.overlaps(b["start_time"], b["end_time"],
                          proposal["new_start"], proposal["new_end"])
        for b in repo.all_bookings()
    )
    if instructor_busy:
        repo.set_proposal_status(proposal_id, "rejected")
        repo.set_conflict_status(conflict["id"], "escalated")
        return {"ok": False, "reason": "instructor no longer free - conflict escalated"}
    repo.update_booking_room(
        proposal["booking_id"], proposal["new_room_id"],
        proposal["new_day"], proposal["new_start"], proposal["new_end"],
    )
    repo.set_proposal_status(proposal_id, "applied")
    for other in repo.proposals_for(proposal["conflict_id"]):
        if other["id"] != proposal_id and other["status"] == "proposed":
            repo.set_proposal_status(other["id"], "rejected")
    repo.set_conflict_status(conflict["id"], "resolved")
    # agents learn: remember where this instructor ended up
    if mover and proposal["new_room_id"]:
        room = repo.get_room(proposal["new_room_id"])
        if room:
            repo.record_building_preference(mover["instructor"], room["building"])
    return {"ok": True, "moved_booking_id": proposal["booking_id"], "proposal": proposal}


def reject_proposal(proposal_id: int) -> dict:
    proposal = repo.get_proposal(proposal_id)
    if not proposal:
        return {"ok": False, "reason": "unknown proposal"}
    repo.set_proposal_status(proposal_id, "rejected")
    repo.set_conflict_status(proposal["conflict_id"], "escalated")
    return {"ok": True}


class CoordinatorAgent(BaseAgent):
    name = "coordinator"

    def __init__(self, llm, bus):
        super().__init__(llm, bus)
        self.graph = self._build_graph()

    # ------------------------------------------------------------ helpers
    def _preferred_building(self, mover: dict) -> str | None:
        """Round-1 preference: the building the instructor was last happiest
        in (learned from accepted reallocations), else their current building."""
        learned = repo.top_building_preference(mover["instructor"])
        return learned or mover["building"]

    def _trace(self, to: str, role: str, topic: str, content: dict) -> dict:
        return {"trace": [{"to": to, "role": role, "topic": topic, "content": content}]}

    def _log_trace(self, state: CoordinatorState, to: str, role: str, topic: str,
                   content: dict) -> dict:
        """Append to the graph trace AND persist on the message bus so the
        dashboard shows the full negotiation narrative."""
        repo.insert_message(state["correlation_id"], self.name, to, role, topic, content)
        return self._trace(to, role, topic, content)

    def _pick_mover(self, bookings: list[dict]) -> int:
        """Move the lower-priority class; on ties, the smaller class / newer booking."""
        ranked = sorted(
            bookings,
            key=lambda b: (PRIORITY_RANK.get(b["priority"], 3), -b["enrolled"], b["id"]),
        )
        return ranked[-1]["id"]

    # ------------------------------------------------------------ nodes
    async def intake(self, state: CoordinatorState) -> dict:
        conflict = repo.get_conflict(state["conflict_id"])
        ids = [conflict["booking_a_id"], conflict["booking_b_id"]]
        bookings = [repo.get_booking_full(i) for i in ids if i]
        bookings = [b for b in bookings if b]
        mover_id = self._pick_mover(bookings)
        cid = f"cf-{conflict['id']}-{uuid.uuid4().hex[:8]}"
        state = {**state, "correlation_id": cid}
        self.log_event("conflict_intake", {"conflict_id": conflict["id"], "type": conflict["type"]})
        return {
            "conflict": conflict,
            "bookings": bookings,
            "mover_id": mover_id,
            "correlation_id": cid,
            "round": 1,
            **self._log_trace(state, "schedule", "ack", "conflict.intake",
                              {"received": conflict["description"], "mover": mover_id}),
        }

    async def gather(self, state: CoordinatorState) -> dict:
        """Ask the Availability Agent for candidate rooms (relaxing constraints each round)."""
        rnd = state["round"]
        mover = next(b for b in state["bookings"] if b["id"] == state["mover_id"])
        exclude = sorted({b["room_id"] for b in state["bookings"]})
        payload = {
            "day": mover["day"],
            "start_time": mover["start_time"],
            "end_time": mover["end_time"],
            "min_capacity": mover["enrolled"],
            "required_equipment": mover["required_equipment"],
            "exclude_room_ids": exclude,
            "prefer_building": None if rnd >= 2 else self._preferred_building(mover),
            "instructor": mover["instructor"],
            "exclude_booking_ids": [b["id"] for b in state["bookings"]],
            "context": {
                "enrolled": mover["enrolled"],
                "required_equipment": mover["required_equipment"],
                "current_building": mover["building"],
                "current_slot": {"day": mover["day"], "start": mover["start_time"],
                                 "end": mover["end_time"]},
            },
        }
        candidates: list[dict] = []
        resp = await self.bus.call(self.name, "availability", "query", payload,
                                   state["correlation_id"])
        if isinstance(resp, dict) and resp.get("candidates"):
            candidates = resp["candidates"]
        if rnd >= 3 and not candidates:
            # last resort: propose time shifts (negotiation concession)
            for shift in (-60, 60, -120, 120):
                new_start = repo.add_minutes(mover["start_time"], shift)
                new_end = repo.add_minutes(mover["end_time"], shift)
                if new_start < "08:00" or new_end > "18:00":
                    continue
                shifted = {**payload, "start_time": new_start, "end_time": new_end,
                           "prefer_building": None}
                resp2 = await self.bus.call(self.name, "availability", "query", shifted,
                                            state["correlation_id"])
                for cand in resp2.get("candidates", []):
                    cand["slot"] = {"day": mover["day"], "start": new_start, "end": new_end}
                    candidates.append(cand)
        for cand in candidates:
            cand.setdefault("slot", {"day": mover["day"], "start": mover["start_time"],
                                     "end": mover["end_time"]})
        return {
            "candidates": candidates,
            **self._log_trace(state, "availability", "note", "candidates.gathered",
                              {"round": rnd, "count": len(candidates),
                               "strategy": "same-slot" if rnd < 3 else "same-slot+time-shifts"}),
        }

    async def propose(self, state: CoordinatorState) -> dict:
        """Turn candidates into scored proposals and let the LLM negotiate."""
        rnd = state["round"]
        conflict = state["conflict"]
        mover = next(b for b in state["bookings"] if b["id"] == state["mover_id"])
        ctx = {
            "enrolled": mover["enrolled"],
            "required_equipment": mover["required_equipment"],
            "current_building": mover["building"],
            "current_slot": {"day": mover["day"], "start": mover["start_time"],
                             "end": mover["end_time"]},
        }
        rows = []
        for idx, cand in enumerate(state.get("candidates", [])):
            slot = cand["slot"]
            score, breakdown = scoring.score_candidate(
                ctx, cand, slot, None if state["round"] >= 2 else mover["building"])
            pid = repo.insert_proposal(
                conflict["id"], rnd, "move_booking", mover["id"], cand["room_id"],
                slot["day"], slot["start"], slot["end"], score,
                json.dumps(breakdown), "", "proposed",
            )
            rows.append({"id": pid, "idx": idx, **cand, "score": score,
                         "breakdown": breakdown, "slot": slot})
        rows.sort(key=lambda r: (-r["score"], r["room_id"]))
        best_row = rows[0] if rows else None
        chosen_index = 0 if rows else -1
        reasoning = (
            f"Rule-based negotiation: selected {best_row['room_name']} "
            f"(score {best_row['score']}) - satisfies capacity "
            f"({mover['enrolled']} students), required equipment "
            f"{mover['required_equipment'] or 'none'}, and keeps the day/time stable."
            if best_row else "No candidate rooms satisfied the hard constraints."
        )
        if self.llm.available() and rows:
            options = [
                {"index": r["idx"], "room": r["room_name"], "building": r["building"],
                 "capacity": r["capacity"], "equipment": r["equipment"], "score": r["score"],
                 "slot": r["slot"]}
                for r in rows
            ]
            prompt = (
                "You are the Coordinator Agent of a smart campus multi-agent system. "
                "A scheduling conflict was detected and you must negotiate the best "
                "reallocation with the Availability Agent's candidates.\n\n"
                f"CONFLICT: {json.dumps({'type': conflict['type'], 'severity': conflict['severity'], 'description': conflict['description']})}\n"
                f"AFFECTED CLASSES: {json.dumps([_brief(b) for b in state['bookings']])}\n"
                f"CANDIDATE OPTIONS: {json.dumps(options)}\n\n"
                "POLICY: priority order exam > core > lab > elective > event; the "
                "lower-priority class should move; keep day/time if possible; capacity "
                "and required equipment are hard constraints.\n\n"
                'Respond ONLY with JSON: {"chosen_index": <int index into CANDIDATE OPTIONS, '
                '-1 if none acceptable>, "shift_minutes": <0, or a proposed time shift '
                'like -60 or 60>, "reasoning": "<max 2 sentences>"}'
            )
            data = self.llm.invoke_json(prompt)
            if data and isinstance(data.get("chosen_index"), int) \
                    and 0 <= data["chosen_index"] < len(rows):
                best_row = rows[data["chosen_index"]]
                chosen_index = data["chosen_index"]
                reasoning = data.get("reasoning") or reasoning
                shift = int(data.get("shift_minutes") or 0)
                if shift:
                    slot = best_row["slot"]
                    s2 = repo.add_minutes(slot["start"], shift)
                    e2 = repo.add_minutes(slot["end"], shift)
                    involved = [b["id"] for b in state["bookings"]]
                    if repo.room_free(best_row["room_id"], slot["day"], s2, e2,
                                      exclude_booking_ids=involved):
                        best_row["slot"] = {"day": slot["day"], "start": s2, "end": e2}
                        score, breakdown = scoring.score_candidate(ctx, best_row, best_row["slot"])
                        repo.update_proposal_score(best_row["id"], score, json.dumps(breakdown))
                        best_row["score"], best_row["breakdown"] = score, breakdown
        if best_row:
            repo.update_proposal_reasoning(best_row["id"], reasoning)
            best_row["reasoning"] = reasoning
        return {
            "proposals": rows,
            "best": best_row,
            **self._log_trace(state, "log", "reasoning", "proposals.created",
                              {"round": rnd, "options": len(rows),
                               "chosen_index": chosen_index, "reasoning": reasoning}),
        }

    async def evaluate(self, state: CoordinatorState) -> dict:
        best = state.get("best") or {}
        self.log_event("options_evaluated", {
            "conflict_id": state["conflict"]["id"],
            "options": len(state.get("proposals", [])),
            "best_score": best.get("score"),
        })
        return self._log_trace(state, "log", "reasoning", "options.evaluated", {
            "options": len(state.get("proposals", [])),
            "best_score": best.get("score"),
            "best_room": best.get("room_name"),
        })

    async def decide(self, state: CoordinatorState) -> dict:
        """Policy gate: auto-apply above threshold unless an exam is involved;
        otherwise keep negotiating (relaxed constraints) or queue for approval."""
        best = state.get("best")
        conflict = state["conflict"]
        rnd = state["round"]
        exam_involved = any(b["priority"] == "exam" for b in state["bookings"])

        if rnd < settings.max_negotiation_rounds and (
                not best or (best["score"] < settings.auto_apply_threshold
                             and not exam_involved)):
            return {"decision": "retry", "round": rnd + 1}

        if best and best["score"] >= settings.auto_apply_threshold and not exam_involved:
            result = apply_proposal(best["id"])
            if result["ok"]:
                decision, moved_id = "auto_applied", result["moved_booking_id"]
                repo.set_conflict_status(conflict["id"], "resolved")
            else:
                decision, moved_id = "failed", None
                repo.set_conflict_status(conflict["id"], "escalated")
        elif best:
            decision, moved_id = "pending_approval", None
            repo.set_proposal_status(best["id"], "awaiting_approval")
            repo.set_conflict_status(conflict["id"], "pending_approval")
        else:
            decision, moved_id = "failed", None
            repo.set_conflict_status(conflict["id"], "escalated")

        self.log_event("decision", {"conflict_id": conflict["id"], "decision": decision})
        return {"decision": decision, "moved_booking_id": moved_id}

    async def notify(self, state: CoordinatorState) -> dict:
        decision = state.get("decision", "failed")
        conflict = state["conflict"]
        mover = next(b for b in state["bookings"] if b["id"] == state["mover_id"])
        best = state.get("best") or {}
        items = []
        if decision == "auto_applied":
            slot = best["slot"]
            items.append({
                "audience": f"students:{mover['code']}",
                "subject": f"Room change for {mover['code']}",
                "body": (f"{mover['code']} ({mover['title']}) on {slot['day']} "
                         f"{slot['start']}-{slot['end']} now takes place in "
                         f"{best['room_name']} (was {mover['room_name']}). The change "
                         f"was applied automatically after agent negotiation."),
            })
            items.append({
                "audience": f"instructor:{mover['instructor']}",
                "subject": f"Room change for {mover['code']}",
                "body": (f"Your {mover['code']} session on {slot['day']} "
                         f"{slot['start']}-{slot['end']} moved to {best['room_name']}."),
            })
            other = next((b for b in state["bookings"] if b["id"] != state["mover_id"]), None)
            if other:
                items.append({
                    "audience": f"students:{other['code']}",
                    "subject": f"Schedule confirmed for {other['code']}",
                    "body": (f"{other['code']} keeps its room ({other['room_name']}) - "
                             f"the overlapping class was relocated by the system."),
                })
        elif decision == "pending_approval":
            items.append({
                "audience": "admin",
                "subject": f"Approval needed - conflict #{conflict['id']}",
                "body": (f"Best proposal for {conflict['description']} scored "
                         f"{best.get('score')}. Review it on the dashboard."),
            })
        else:
            items.append({
                "audience": "admin",
                "subject": f"Conflict #{conflict['id']} escalated",
                "body": f"No acceptable reallocation was found automatically for: "
                        f"{conflict['description']}",
            })
        resp = await self.bus.call(self.name, "notifications", "dispatch",
                                   {"conflict_id": conflict["id"], "items": items},
                                   state["correlation_id"])
        sent = resp.get("sent") if isinstance(resp, dict) else None
        return self._log_trace(state, "notifications", "note", "decision.notifications",
                               {"decision": decision, "notifications_sent": sent})

    # ------------------------------------------------------------ graph
    def _build_graph(self):
        g = StateGraph(CoordinatorState)
        g.add_node("intake", self.intake)
        g.add_node("gather", self.gather)
        g.add_node("propose", self.propose)
        g.add_node("evaluate", self.evaluate)
        g.add_node("decide", self.decide)
        g.add_node("notify", self.notify)
        g.add_edge(START, "intake")
        g.add_edge("intake", "gather")
        g.add_edge("gather", "propose")
        g.add_edge("propose", "evaluate")
        g.add_edge("evaluate", "decide")
        g.add_conditional_edges(
            "decide",
            lambda s: "gather" if s.get("decision") == "retry" else "notify",
            {"gather": "gather", "notify": "notify"},
        )
        g.add_edge("notify", END)
        return g.compile()

    async def handle_conflict(self, conflict_id: int) -> dict:
        final = await self.graph.ainvoke({"conflict_id": conflict_id, "trace": []})
        return {
            "conflict_id": conflict_id,
            "decision": final.get("decision"),
            "best": final.get("best") or {},
            "moved_booking_id": final.get("moved_booking_id"),
            "trace": final.get("trace", []),
        }

    async def handle_conflict_payload(self, payload: dict) -> dict:
        return await self.handle_conflict(int(payload["conflict_id"]))

    async def handle_conflicts_batch(self, conflict_ids: list) -> dict:
        """Negotiate several conflicts together: one global gather across all
        displaced classes, a greedy conflict-free allocation (a room claimed
        by one move cannot be claimed by another in the same round), then a
        guardrailed application of each move with one summary notification."""
        cid = f"batch-{uuid.uuid4().hex[:8]}"
        conflicts = [repo.get_conflict(i) for i in conflict_ids]
        conflicts = [c for c in conflicts if c and c["status"] == "open"]
        assignments, unresolved = [], []
        claimed: list = []  # (room_id, day, start, end) reserved in this batch

        for conflict in conflicts:
            involved = [conflict["booking_a_id"], conflict["booking_b_id"]]
            bookings = [repo.get_booking_full(i) for i in involved if i]
            bookings = [b for b in bookings if b]
            if not bookings:
                unresolved.append({"conflict_id": conflict["id"], "reason": "no bookings"})
                continue
            mover = next(b for b in bookings if b["id"] == self._pick_mover(bookings))
            response = await self.bus.call(self.name, "availability", "query", {
                "day": mover["day"],
                "start_time": mover["start_time"],
                "end_time": mover["end_time"],
                "min_capacity": mover["enrolled"],
                "required_equipment": mover["required_equipment"],
                "exclude_room_ids": sorted({b["room_id"] for b in bookings}),
                "prefer_building": self._preferred_building(mover),
                "instructor": mover["instructor"],
                "exclude_booking_ids": [b["id"] for b in bookings],
                "context": {
                    "enrolled": mover["enrolled"],
                    "required_equipment": mover["required_equipment"],
                    "current_building": mover["building"],
                    "current_slot": {"day": mover["day"], "start": mover["start_time"],
                                     "end": mover["end_time"]},
                },
            }, cid)
            candidates = response.get("candidates", []) if isinstance(response, dict) else []

            placed = False
            for candidate in candidates:
                slot = candidate["slot"]
                claimed_here = any(
                    candidate["room_id"] == room_id and day == mover["day"]
                    and repo.overlaps(start, end, slot["start"], slot["end"])
                    for room_id, day, start, end in claimed)
                if claimed_here:
                    continue
                score, breakdown = scoring.score_candidate({
                    "enrolled": mover["enrolled"],
                    "required_equipment": mover["required_equipment"],
                    "current_building": mover["building"],
                    "current_slot": {"day": mover["day"], "start": mover["start_time"],
                                     "end": mover["end_time"]},
                }, candidate, slot, self._preferred_building(mover))
                proposal_id = repo.insert_proposal(
                    conflict["id"], 1, "move_booking", mover["id"], candidate["room_id"],
                    slot["day"], slot["start"], slot["end"], score,
                    json.dumps(breakdown), "Batch allocation by the Coordinator Agent.", "proposed")
                result = apply_proposal(proposal_id)
                if result["ok"]:
                    claimed.append((candidate["room_id"], mover["day"],
                                    slot["start"], slot["end"]))
                    assignments.append({"conflict_id": conflict["id"], "course": mover["code"],
                                        "room": candidate["room_name"], "score": score,
                                        "proposal_id": proposal_id})
                    placed = True
                    break
            if not placed:
                unresolved.append({"conflict_id": conflict["id"], "course": mover["code"]})
                if repo.get_conflict(conflict["id"])["status"] == "open":
                    repo.set_conflict_status(conflict["id"], "escalated")

        items = []
        if assignments:
            items.append({
                "audience": "admin",
                "subject": f"Batch round settled {len(assignments)} conflict(s)",
                "body": ("One negotiation pass reallocated: "
                         + "; ".join(f"{a['course']} to {a['room']}" for a in assignments)),
            })
        if unresolved:
            items.append({
                "audience": "admin",
                "subject": "Batch round left conflicts open",
                "body": ("No safe reallocation found for: "
                         + ", ".join(f"#{u['conflict_id']} ({u.get('course', '?')})"
                                     for u in unresolved)),
            })
        if items:
            await self.bus.call(self.name, "notifications", "dispatch",
                                {"conflict_id": None, "items": items}, cid)
        self.log_event("batch_negotiation",
                       {"applied": len(assignments), "unresolved": len(unresolved)})
        return {"applied": assignments, "unresolved": unresolved}