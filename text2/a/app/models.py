from datetime import timezone

from sqlalchemy import (JSON, Boolean, Column, Date, DateTime, ForeignKey,
                        Integer, String, Time, UniqueConstraint)
from sqlalchemy.orm import relationship
from sqlalchemy.types import TypeDecorator

from app.database import Base


class UTCDateTime(TypeDecorator):
    """Stores everything as UTC; returns tz-aware datetimes on every backend
    (SQLite drops tzinfo, PostgreSQL timestamptz keeps it)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class CarePlan(Base):
    __tablename__ = "care_plans"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    unit_id = Column(String(64), nullable=False, index=True)  # 授权单元
    patient_name = Column(String(200), nullable=False)
    timezone = Column(String(64), nullable=False, default="UTC")  # 服务时区 (IANA)
    frequency = Column(String(16), nullable=False)  # daily | weekly
    interval = Column(Integer, nullable=False, default=1)
    byweekday = Column(JSON, nullable=True)  # ["MO", "WE", ...] for weekly
    window_start = Column(Time, nullable=False)  # 时间窗口（本地时间）
    window_end = Column(Time, nullable=False)
    duration_minutes = Column(Integer, nullable=False)  # 时长
    required_qualification = Column(String(64), nullable=False)  # 所需资格
    prerequisite_plan_id = Column(Integer, ForeignKey("care_plans.id"), nullable=True)  # 前置任务
    start_date = Column(Date, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    active = Column(Boolean, nullable=False, default=True)

    tasks = relationship("CareTask", back_populates="plan",
                         foreign_keys="CareTask.plan_id")


class CareTask(Base):
    __tablename__ = "care_tasks"
    # 幂等键：同一计划、同一版本、同一开始时刻只建一单
    __table_args__ = (
        UniqueConstraint("plan_id", "plan_version", "scheduled_start",
                         name="uq_task_occurrence"),
    )

    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("care_plans.id"), nullable=False, index=True)
    plan_version = Column(Integer, nullable=False)
    scheduled_start = Column(UTCDateTime, nullable=False)
    scheduled_end = Column(UTCDateTime, nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    # pending -> offered -> assigned -> completed; cancelled 为终止态
    required_qualification = Column(String(64), nullable=False)
    unit_id = Column(String(64), nullable=False, index=True)
    depends_on_task_id = Column(Integer, ForeignKey("care_tasks.id"), nullable=True)

    plan = relationship("CarePlan", back_populates="tasks", foreign_keys=[plan_id])
    offers = relationship("Offer", back_populates="task")
    assignment = relationship("Assignment", back_populates="task", uselist=False)


class Caregiver(Base):
    __tablename__ = "caregivers"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    unit_id = Column(String(64), nullable=False, index=True)

    qualifications = relationship("CaregiverQualification", back_populates="caregiver")


class CaregiverQualification(Base):
    __tablename__ = "caregiver_qualifications"

    id = Column(Integer, primary_key=True)
    caregiver_id = Column(Integer, ForeignKey("caregivers.id"), nullable=False, index=True)
    code = Column(String(64), nullable=False)
    valid_from = Column(UTCDateTime, nullable=False)
    valid_until = Column(UTCDateTime, nullable=False)

    caregiver = relationship("Caregiver", back_populates="qualifications")


class Offer(Base):
    """一次邀请。8 分钟未接受即失效。"""

    __tablename__ = "offers"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("care_tasks.id"), nullable=False, index=True)
    caregiver_id = Column(Integer, ForeignKey("caregivers.id"), nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    # pending | accepted | rejected | expired | superseded
    offered_at = Column(UTCDateTime, nullable=False)
    expires_at = Column(UTCDateTime, nullable=False)
    responded_at = Column(UTCDateTime, nullable=True)

    task = relationship("CareTask", back_populates="offers")


class Assignment(Base):
    """有效分配：一个任务至多一条（task_id 唯一约束兜底并发）。"""

    __tablename__ = "assignments"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("care_tasks.id"), nullable=False, unique=True)
    caregiver_id = Column(Integer, ForeignKey("caregivers.id"), nullable=False)
    source = Column(String(16), nullable=False, default="offer")  # offer | manual
    created_by = Column(String(64), nullable=False, default="system")
    created_at = Column(UTCDateTime, nullable=False)

    task = relationship("CareTask", back_populates="assignment")


class Coordinator(Base):
    __tablename__ = "coordinators"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)

    units = relationship("CoordinatorUnit", back_populates="coordinator")


class CoordinatorUnit(Base):
    """协调员被授权的单元。"""

    __tablename__ = "coordinator_units"

    coordinator_id = Column(Integer, ForeignKey("coordinators.id"), primary_key=True)
    unit_id = Column(String(64), primary_key=True)

    coordinator = relationship("Coordinator", back_populates="units")


class AdjustmentLog(Base):
    """变更历史：自动与人工调整都记录。"""

    __tablename__ = "adjustment_logs"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("care_tasks.id"), nullable=False, index=True)
    actor = Column(String(64), nullable=False)  # system | coordinator:<id>
    action = Column(String(32), nullable=False)
    reason = Column(String(500), nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(UTCDateTime, nullable=False)
