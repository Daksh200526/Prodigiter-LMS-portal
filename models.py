from datetime import datetime
from enum import Enum
import uuid
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from database import Base


class Role(str, Enum):
    ADMIN = "admin"
    TRAINER = "trainer"
    STUDENT = "student"


class AssignmentStatus(str, Enum):
    NOT_STARTED = "not_started"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"


class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    role = Column(String(20), default=Role.STUDENT.value, index=True)
    name = Column(String(100), nullable=False)
    email = Column(String(191), unique=True, index=True, nullable=False)
    specialization = Column(String(255), nullable=True)
    course_subject = Column(String(100), nullable=True)
    phone_number = Column(String(20), unique=True, nullable=True)
    hashed_password = Column(String(255), nullable=True)

    is_profile_complete = Column(Boolean, default=False)
    is_access_granted = Column(Boolean, default=False)
    last_video_watched_at = Column(DateTime, nullable=True)
    registered_at = Column(DateTime, default=datetime.utcnow, nullable=True)

    courses_taught = relationship("Course", back_populates="trainer")
    enrollments = relationship("Enrollment", back_populates="student", cascade="all, delete-orphan")
    lesson_questions = relationship(
        "LessonQuestion",
        back_populates="student",
        cascade="all, delete-orphan",
    )


class Course(Base):
    __tablename__ = "courses"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    trainer_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    trainer = relationship("User", back_populates="courses_taught")
    modules = relationship("Module", back_populates="course", cascade="all, delete-orphan", order_by="Module.order_index")
    enrollments = relationship("Enrollment", back_populates="course")


class Module(Base):
    __tablename__ = "modules"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    course_id = Column(String(36), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    order_index = Column(Integer, nullable=False)
    title = Column(String(255), nullable=False)
    video_url = Column(String(500), nullable=False)
    assignment_prompt = Column(Text, nullable=False)
    assignment_link = Column(String(500), nullable=True)

    course = relationship("Course", back_populates="modules")
    progress_records = relationship("StudentProgress", back_populates="module")
    lesson_questions = relationship(
        "LessonQuestion",
        back_populates="module",
        cascade="all, delete-orphan",
        order_by="LessonQuestion.created_at",
    )


class Enrollment(Base):
    __tablename__ = "enrollments"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    student_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    course_id = Column(String(36), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    enrolled_at = Column(DateTime, default=datetime.utcnow)
    project_index = Column(Integer, nullable=True)
    is_completed = Column(Boolean, default=False)
    certificate_id = Column(String(50), nullable=True)

    student = relationship("User", back_populates="enrollments")
    course = relationship("Course", back_populates="enrollments")
    progress_list = relationship("StudentProgress", back_populates="enrollment", cascade="all, delete-orphan")


class StudentProgress(Base):
    __tablename__ = "progress"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    enrollment_id = Column(String(36), ForeignKey("enrollments.id", ondelete="CASCADE"), nullable=False)
    module_id = Column(String(36), ForeignKey("modules.id", ondelete="CASCADE"), nullable=False)

    is_video_completed = Column(Boolean, default=False)
    video_completed_at = Column(DateTime, nullable=True)
    assignment_completed = Column(Boolean, default=False, nullable=False)
    assignment_completed_at = Column(DateTime, nullable=True)
    review_answers = Column(Text, nullable=True)
    generated_review = Column(Text, nullable=True)
    review_generated_at = Column(DateTime, nullable=True)

    assignment_status = Column(String(30), default=AssignmentStatus.NOT_STARTED.value)
    is_approved = Column(Boolean, default=False)
    submission_notes = Column(Text, nullable=True)
    submission_file_path = Column(String(500), nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    evaluated_at = Column(DateTime, nullable=True)
    trainer_feedback = Column(Text, nullable=True)

    enrollment = relationship("Enrollment", back_populates="progress_list")
    module = relationship("Module", back_populates="progress_records")


class LessonQuestion(Base):
    __tablename__ = "lesson_questions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    module_id = Column(String(36), ForeignKey("modules.id", ondelete="CASCADE"), nullable=False, index=True)
    student_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    question = Column(Text, nullable=False)
    trainer_reply = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    replied_at = Column(DateTime, nullable=True)

    module = relationship("Module", back_populates="lesson_questions")
    student = relationship("User", back_populates="lesson_questions")


class OTPChallenge(Base):
    __tablename__ = "otp_challenges"

    phone_hash = Column(String(64), primary_key=True)
    otp_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    sent_at = Column(DateTime, nullable=False, index=True)
    sends_in_hour = Column(Integer, default=1, nullable=False)
    send_window_started_at = Column(DateTime, nullable=False)


class AuthRateLimit(Base):
    __tablename__ = "auth_rate_limits"

    key = Column(String(64), primary_key=True)
    attempts = Column(Integer, nullable=False)
    window_started_at = Column(DateTime, nullable=False, index=True)


class DeletionRequest(Base):
    __tablename__ = "deletion_requests"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    student_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    trainer_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reason = Column(Text, nullable=False)
    is_approved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    student = relationship("User", foreign_keys=[student_id])
    trainer = relationship("User", foreign_keys=[trainer_id])