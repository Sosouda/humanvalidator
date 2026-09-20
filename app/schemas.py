from pydantic import BaseModel
from datetime import datetime, date
from typing import Optional, List

class EmployeeOut(BaseModel):
    id: int
    full_name: str
    aliases: str
    telegram_user_id: Optional[int]
    telegram_username: Optional[str]
    class Config: from_attributes = True

class GroupOut(BaseModel):
    id: int
    tg_id: int
    title: str
    facility_id: Optional[int]
    is_enabled: bool
    class Config: from_attributes = True

class ShiftMarkOut(BaseModel):
    id: int
    employee_id: int
    shift_date: date
    facility_id: Optional[int]
    value: int
    message_id: Optional[str]
    created_at: datetime
    class Config: from_attributes = True

class MessageLogOut(BaseModel):
    id: int
    tg_message_id: int
    tg_chat_id: int
    date_time: datetime
    has_photo: bool
    caption: str
    from_user_id: Optional[int]
    status: str
    reason: str
    base_link: str
    employee_id: Optional[int]
    shift_date: Optional[date]
    class Config: from_attributes = True

class IngestMessage(BaseModel):
    tg_message_id: int
    tg_chat_id: int
    date_time: datetime
    has_photo: bool
    caption: Optional[str] = ""
    from_user_id: Optional[int] = None
    from_username: Optional[str] = None
    file_id: Optional[str] = None

class ShiftTableRow(BaseModel):
    employee: str
    days: dict  # day -> 1/0
    total: int
