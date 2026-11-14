"""Transparent weighted scoring used by the Availability and Coordinator agents.

Every candidate reallocation gets a 0..1 score with a full breakdown, so
decisions are explainable. The LLM never invents scores - it only reasons
about these pre-scored options.
"""

WEIGHTS = {
    "capacity": 0.35,
    "equipment": 0.25,
    "proximity": 0.15,
    "time": 0.15,
    "priority": 0.10,
}


def capacity_fit(capacity: int, enrolled: int) -> float:
    if capacity < enrolled:
        return 0.0
    if capacity > 1.8 * enrolled:      # wastefully large hall
        return 0.65
    if capacity > 1.4 * enrolled:
        return 0.85
    return 1.0


def equipment_fit(room_equipment: list, required: list) -> float:
    if not required:
        return 1.0
    matched = sum(1 for item in required if item in room_equipment)
    return matched / len(required)


def proximity_fit(room_building: str, current_building: str | None, prefer_building: str | None) -> float:
    if prefer_building is None:        # constraint relaxed during negotiation
        return 1.0
    if not current_building:
        return 1.0
    return 1.0 if room_building == prefer_building else 0.5


def time_fit(slot: dict, current_slot: dict | None) -> float:
    if not current_slot:
        return 1.0
    same = slot["day"] == current_slot["day"] and slot["start"] == current_slot["start"]
    return 1.0 if same else 0.7


def score_candidate(ctx: dict, room: dict, slot: dict, prefer_building: str | None = None):
    """ctx: {enrolled, required_equipment, current_building, current_slot}."""
    cap = capacity_fit(room["capacity"], ctx["enrolled"])
    eq = equipment_fit(room.get("equipment", []), ctx.get("required_equipment", []))
    prox = proximity_fit(room["building"], ctx.get("current_building"), prefer_building)
    t = time_fit(slot, ctx.get("current_slot"))
    score = (
        WEIGHTS["capacity"] * cap
        + WEIGHTS["equipment"] * eq
        + WEIGHTS["proximity"] * prox
        + WEIGHTS["time"] * t
        + WEIGHTS["priority"] * 1.0  # neutral; priority is enforced by policy gate
    )
    breakdown = {
        "capacity": round(cap, 2),
        "equipment": round(eq, 2),
        "proximity": round(prox, 2),
        "time": round(t, 2),
        "priority": 1.0,
    }
    return round(score, 4), breakdown
