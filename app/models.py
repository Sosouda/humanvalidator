from sqlalchemy import Column, Integer, String, DateTime, Date, Boolean, Text, ForeignKey, UniqueConstraint, Index, Float
from sqlalchemy.orm import relationship, Mapped, mapped_column
from datetime import datetime, date
from .database import Base
import enum

class ProcessingStatus(str, enum.Enum):
    accepted = "принято"
    rejected = "отклонено"
    manual = "ручная_проверка"
    error = "ошибка"
    duplicate = "дубликат"

# 5. Объекты учета
class Facility(Base):
    __tablename__ = "facilities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    address: Mapped[str] = mapped_column(String(512), nullable=True)
    # допустимое окно времени для отметки, напр. 06:00-10:00, 18:00-22:00
    # храним как строка "06:00-10:00,18:00-22:00"
    time_windows: Mapped[str] = mapped_column(String(255), default="06:00-12:00")
    # смещение для полуночи: если смена ночная, к какому дню относится
    # midnight_cutoff в часах, напр. 4 означает что 00:00-04:00 относится к предыдущему дню
    midnight_cutoff_hour: Mapped[int] = mapped_column(Integer, default=4)
    # флаг из ответа §6: если False — объект нельзя автоотметить, всё уходит на ручную проверку
    auto_mark_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    groups = relationship("TelegramGroup", back_populates="facility")

class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(255), index=True)  # "Мехоношин" — неуникально, бывают однофамильцы/братья
    aliases: Mapped[str] = mapped_column(Text, default="")  # JSON list строк: позывные, табельные
    telegram_user_id: Mapped[int] = mapped_column(Integer, nullable=True, unique=False)
    telegram_username: Mapped[str] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # для различения однофамильцев можно использовать табельный/объект, но ФИО не уникально
    __table_args__ = (
        Index("ix_employee_full_name", "full_name"),
    )

class TelegramGroup(Base):
    __tablename__ = "telegram_groups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(Integer, unique=True)  # chat.id (negative for supergroup)
    title: Mapped[str] = mapped_column(String(255))
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # ограничение по сотрудникам: если задано, только они могут отмечаться из этой группы
    allowed_employee_ids: Mapped[str] = mapped_column(Text, default="")  # JSON list ids
    # тип группы для генерации ссылок §7: supergroup/channel имеют t.me/c/, обычная группа - нет
    group_type: Mapped[str] = mapped_column(String(32), default="supergroup")  # supergroup/channel/group
    invite_link: Mapped[str] = mapped_column(String(512), default="")
    facility = relationship("Facility", back_populates="groups")

class ShiftMark(Base):
    __tablename__ = "shift_marks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    shift_date: Mapped[date] = mapped_column(Date, index=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"), nullable=True)
    value: Mapped[int] = mapped_column(Integer, default=1)
    message_id: Mapped[str] = mapped_column(String(64), nullable=True)  # tg message id + chat
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    auto_created: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (
        UniqueConstraint("employee_id", "shift_date", "facility_id", name="uq_emp_date_fac"),
        Index("ix_shift_date", "shift_date"),
    )
    employee = relationship("Employee")
    facility = relationship("Facility")

class MessageLog(Base):
    __tablename__ = "message_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_message_id: Mapped[int] = mapped_column(Integer)
    tg_chat_id: Mapped[int] = mapped_column(Integer)
    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id"), nullable=True)
    date_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    has_photo: Mapped[bool] = mapped_column(Boolean, default=False)
    caption: Mapped[str] = mapped_column(Text, default="")
    from_user_id: Mapped[int] = mapped_column(Integer, nullable=True)
    from_username: Mapped[str] = mapped_column(String(255), nullable=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=True)
    shift_date: Mapped[date] = mapped_column(Date, nullable=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=ProcessingStatus.manual.value)
    reason: Mapped[str] = mapped_column(Text, default="")
    # основание: ссылка t.me/c/... или комментарий
    base_link: Mapped[str] = mapped_column(String(512), default="")
    # данные vision/OCR: распознанная дата с фото, результат нейронки
    photo_ocr_text: Mapped[str] = mapped_column(Text, default="")
    photo_ocr_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    vision_person_detected: Mapped[bool] = mapped_column(Boolean, nullable=True)
    vision_reason: Mapped[str] = mapped_column(Text, default="")
    raw_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("tg_chat_id", "tg_message_id", name="uq_tg_msg"),
    )

class UserRole(str, enum.Enum):
    admin = "admin"
    operator = "operator"
    observer = "observer"

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default=UserRole.observer.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64))  # created, updated, deleted, reviewed
    entity: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[int] = mapped_column(Integer, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    user: Mapped[str] = mapped_column(String(255), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
