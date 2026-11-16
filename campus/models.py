"""Pydantic request models for the public API."""
from typing import Literal, Optional

from pydantic import BaseModel, Field


class SeedRequest(BaseModel):
    force: bool = False


class InjectRequest(BaseModel):
    kind: Literal["double_booking", "capacity_overflow", "equipment_mismatch"] = "double_booking"


class AvailabilityQuery(BaseModel):
    day: str = "Mon"
    start_time: str = "08:00"
    end_time: str = "10:00"
    min_capacity: int = Field(1, ge=1)
    required_equipment: list[str] = Field(default_factory=list)
    exclude_room_ids: list[int] = Field(default_factory=list)
    prefer_building: Optional[str] = None
    context: dict = Field(default_factory=dict)


class ChatRequest(BaseModel):
    session_id: str = "web"
    message: str


class RoomStatusRequest(BaseModel):
    status: Literal["active", "maintenance"]


class BookingRequest(BaseModel):
    course_code: str
    room_name: str
    day: str
    start_time: str
    end_time: str


class RoomRequest(BaseModel):
    course_code: str
    day: str
    start_time: str
    end_time: str


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class EnrollRequest(BaseModel):
    course_code: str
