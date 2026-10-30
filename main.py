import os
import re
import datetime
import base64
import hashlib
import hmac
import io
import json
import time
from datetime import timezone, timedelta
from typing import Optional
from urllib.parse import urlsplit
import uuid

from fastapi import FastAPI, Depends, HTTPException, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, ValidationError
from sqlalchemy import func, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, joinedload, selectinload

import models
import auth
import schemas
from config import settings
from database import SessionLocal, engine, get_db
from review_service import REVIEW_QUESTIONS, ReviewProviderError, generate_review
from utils import PROJECT_ASSIGNMENT_COUNT, get_project_assignment_link
import qrcode
from whatsapp_service import (
    WhatsAppError,
    is_whatsapp_delivery_configured,
    send_whatsapp_otp,
)
from otp_security import (
    normalize_phone,
    generate_secure_otp,
    check_rate_limit,
    save_otp,
    discard_otp,
    verify_and_burn_otp,
    RESEND_COOLDOWN_SECONDS,
)


models.Base.metadata.create_all(bind=engine)

if "assignment_completed" not in {
    column["name"] for column in inspect(engine).get_columns("progress")
}:
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE progress ADD COLUMN assignment_completed BOOLEAN NOT NULL DEFAULT 0"
        ))

if "specialization" not in {
    column["name"] for column in inspect(engine).get_columns("users")
}:
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE users ADD COLUMN specialization VARCHAR(255)"
        ))

if "course_subject" not in {
    column["name"] for column in inspect(engine).get_columns("users")
}:
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE users ADD COLUMN course_subject VARCHAR(100)"
        ))

if "registered_at" not in {
    column["name"] for column in inspect(engine).get_columns("users")
}:
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE users ADD COLUMN registered_at DATETIME"
        ))

if "project_index" not in {
    column["name"] for column in inspect(engine).get_columns("enrollments")
}:
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE enrollments ADD COLUMN project_index INTEGER"
        ))

progress_columns = {
    column["name"] for column in inspect(engine).get_columns("progress")
}
for column_name, column_definition in (
    ("assignment_completed_at", "DATETIME"),
    ("review_answers", "TEXT"),
    ("generated_review", "TEXT"),
    ("review_generated_at", "DATETIME"),
):
    if column_name not in progress_columns:
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    f"ALTER TABLE progress ADD COLUMN {column_name} {column_definition}"
                ))
        except OperationalError:
            if column_name not in {
                column["name"] for column in inspect(engine).get_columns("progress")
            }:
                raise

with engine.begin() as connection:
    connection.execute(text(
        "UPDATE progress SET assignment_completed_at = "
        "COALESCE(submitted_at, evaluated_at, video_completed_at) "
        "WHERE assignment_completed = 1 AND assignment_completed_at IS NULL"
    ))

app = FastAPI(title="prodigiter LMS Portal")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("templates", exist_ok=True)
app.mount("/static", StaticFiles(directory="templates"), name="static")

COURSE_SUBJECTS = (
    "Web Development",
    "Python Programming",
    "Data Science",
    "Java Programming",
    "Cloud Computing",
)


def normalize_course_subject(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.split()).casefold()
    return next(
        (subject for subject in COURSE_SUBJECTS if subject.casefold() == normalized),
        None,
    )


def _backfill_enrollment_project_links() -> None:
    db = SessionLocal()
    try:
        enrollments = db.query(models.Enrollment).order_by(
            models.Enrollment.course_id,
            models.Enrollment.enrolled_at,
            models.Enrollment.id,
        ).all()
        project_indexes: dict[str, int] = {}
        updated = False
        for enrollment in enrollments:
            project_index = project_indexes.get(enrollment.course_id, 0)
            if enrollment.project_index is None:
                enrollment.project_index = (
                    project_index % PROJECT_ASSIGNMENT_COUNT
                )
                updated = True
            project_indexes[enrollment.course_id] = project_index + 1
        if updated:
            db.commit()
    finally:
        db.close()


_backfill_enrollment_project_links()


def _enroll_student_in_course(
    db: Session,
    student: models.User,
    course: models.Course,
) -> None:
    existing_enrollment = db.query(models.Enrollment).filter_by(
        student_id=student.id,
        course_id=course.id,
    ).first()
    if existing_enrollment:
        return

    enrollment_count = db.query(func.count(models.Enrollment.id)).filter_by(
        course_id=course.id,
    ).scalar() or 0
    db.add(models.Enrollment(
        id=str(uuid.uuid4()),
        student_id=student.id,
        course_id=course.id,
        project_index=enrollment_count % PROJECT_ASSIGNMENT_COUNT,
    ))
    db.flush()


def _enroll_subject_students_in_course(db: Session, course: models.Course) -> None:
    subject = normalize_course_subject(course.trainer.specialization if course.trainer else None)
    if not subject:
        return
    students = db.query(models.User).filter_by(
        role="student",
        course_subject=subject,
    ).order_by(
        models.User.registered_at,
        models.User.id,
    ).all()
    for student in students:
        _enroll_student_in_course(db, student, course)


def _student_matches_course_subject(student: models.User, course: models.Course) -> bool:
    if not student.course_subject:
        return True
    trainer_subject = normalize_course_subject(
        course.trainer.specialization if course.trainer else None
    )
    return trainer_subject == normalize_course_subject(student.course_subject)


def _lesson_question_payload(question: models.LessonQuestion) -> dict:
    return {
        "id": question.id,
        "student_name": question.student.name,
        "question": question.question,
        "trainer_reply": question.trainer_reply,
        "created_at": question.created_at.isoformat() if question.created_at else None,
        "replied_at": question.replied_at.isoformat() if question.replied_at else None,
    }


class StudentRegister(BaseModel):
    name: str
    email: EmailStr
    phone_number: str
    password: str
    course_subject: str

class PasswordLogin(BaseModel):
    email: EmailStr
    password: str

class OTPRequest(BaseModel):
    phone_number: str

class OTPVerify(BaseModel):
    phone_number: str
    otp_code: str

class AssignmentEvaluation(BaseModel):
    progress_id: str
    is_approved: bool
    feedback: Optional[str] = ""

class AssignmentCompletionWebhook(BaseModel):
    enrollment_id: str
    module_id: str
    completed: bool

class DeletionProcess(BaseModel):
    request_id: str
    approved: bool


@app.get("/")
def serve_index():
    return FileResponse("templates/index.html")


@app.get("/courses/subjects")
def list_course_subjects():
    return {"subjects": COURSE_SUBJECTS}


@app.post("/auth/register-student")
def register_student(payload: StudentRegister, db: Session = Depends(get_db)):
    selected_subject = normalize_course_subject(payload.course_subject)
    if not selected_subject:
        raise HTTPException(
            status_code=422,
            detail="Choose a course from the available course options.",
        )
    if len(payload.password.encode("utf-8")) > 1024:
        raise HTTPException(status_code=400, detail="Password must be 1024 bytes or fewer.")
    clean_phone = normalize_phone(payload.phone_number)
    if not clean_phone:
        raise HTTPException(status_code=400, detail="Invalid Indian mobile number format.")

    existing = db.query(models.User).filter(
        (models.User.email == payload.email) | (models.User.phone_number.like(f"%{clean_phone}"))
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Account with this email or phone number already exists.")

    hashed_pw = auth.get_password_hash(payload.password)
    user = models.User(
        id=str(uuid.uuid4()),
        name=payload.name,
        email=payload.email,
        phone_number=clean_phone,
        hashed_password=hashed_pw,
        course_subject=selected_subject,
        role="student",
        is_access_granted=False
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="An account with this email or phone number already exists.",
        ) from None
    db.refresh(user)
    trainers = db.query(models.User).filter_by(role="trainer").all()
    for trainer in trainers:
        if normalize_course_subject(trainer.specialization) != selected_subject:
            continue
        for course in trainer.courses_taught:
            _enroll_student_in_course(db, user, course)
    db.commit()

    token = auth.create_access_token({"sub": user.id, "role": user.role})
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": user.role,
        "user_id": user.id
    }


@app.post("/auth/login-password")
def login_password(
    payload: PasswordLogin,
    request: Request,
    db: Session = Depends(get_db),
):
    client_ip = request.client.host if request.client else "unknown"
    email = str(payload.email).strip().lower()
    if not auth.record_rate_limit_attempt(db, "password-email", email, 10):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    if not auth.record_rate_limit_attempt(db, "password-ip", client_ip, 30):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")

    user = db.query(models.User).filter(func.lower(models.User.email) == email).first()
    if not user or not auth.verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    if auth.is_legacy_password_hash(user.hashed_password):
        user.hashed_password = auth.get_password_hash(payload.password)
        db.commit()

    token = auth.create_access_token({"sub": user.id, "role": user.role})
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": user.role,
        "user_id": user.id
    }


@app.post("/auth/request-otp")
def request_otp(
    payload: OTPRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    client_ip = request.client.host if request.client else "unknown"
    if not auth.record_rate_limit_attempt(db, "otp-send-ip", client_ip, 10, 3600):
        raise HTTPException(status_code=429, detail="Too many OTP requests. Try again later.")

    clean_phone = normalize_phone(payload.phone_number)
    if not clean_phone:
        raise HTTPException(status_code=400, detail="Please enter a valid 10-digit Indian phone number.")
    if not settings.DEV_MODE and not is_whatsapp_delivery_configured():
        raise HTTPException(
            status_code=503,
            detail="WhatsApp OTP delivery is not configured. Set the required WhatsApp environment variables.",
        )

    user = db.query(models.User).filter_by(phone_number=clean_phone).first()
    if user or settings.DEV_MODE:
        allowed, err_msg = check_rate_limit(db, clean_phone)
        if not allowed:
            raise HTTPException(status_code=429, detail=err_msg)

        otp_code = generate_secure_otp()
        save_otp(db, clean_phone, otp_code)
        if not settings.DEV_MODE and user:
            try:
                send_whatsapp_otp(clean_phone, otp_code)
            except WhatsAppError as error:
                discard_otp(db, clean_phone)
                raise HTTPException(
                    status_code=502,
                    detail=str(error),
                ) from None

    response = {
        "message": (
            "Development mode: use the displayed OTP to continue."
            if settings.DEV_MODE
            else (
                "If this number is registered, an OTP request was processed. "
                "WhatsApp delivery can take time; check the recipient's WhatsApp messages if it does not arrive."
            )
        ),
        "cooldown": RESEND_COOLDOWN_SECONDS
    }
    if settings.DEV_MODE:
        response["development_otp"] = otp_code
    return response


@app.post("/auth/verify-otp")
def verify_otp(
    payload: OTPVerify,
    request: Request,
    db: Session = Depends(get_db),
):
    client_ip = request.client.host if request.client else "unknown"
    if not auth.record_rate_limit_attempt(db, "otp-verify-ip", client_ip, 30, 3600):
        raise HTTPException(status_code=429, detail="Too many verification attempts. Try again later.")

    clean_phone = normalize_phone(payload.phone_number)
    clean_otp = payload.otp_code.strip()

    if not clean_phone or not re.fullmatch(r"\d{6}", clean_otp):
        raise HTTPException(status_code=400, detail="Invalid phone number or 6-digit OTP code.")

    if not auth.record_rate_limit_attempt(
        db, "otp-verify-phone", clean_phone, 10
    ):
        raise HTTPException(status_code=429, detail="Too many verification attempts. Try again later.")
    valid, err_msg = verify_and_burn_otp(db, clean_phone, clean_otp)
    if not valid:
        raise HTTPException(status_code=400, detail=err_msg)

    user = db.query(models.User).filter_by(phone_number=clean_phone).first()
    if not user:
        raise HTTPException(status_code=404, detail="Account not found.")

    token = auth.create_access_token({"sub": user.id, "role": user.role})
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": user.role,
        "user_id": user.id
    }


@app.post("/auth/reset-password")
def reset_password(
    payload: schemas.PasswordReset,
    request: Request,
    db: Session = Depends(get_db),
):
    client_ip = request.client.host if request.client else "unknown"
    if not auth.record_rate_limit_attempt(db, "password-reset-ip", client_ip, 10, 3600):
        raise HTTPException(status_code=429, detail="Too many password reset attempts. Try again later.")

    clean_phone = normalize_phone(payload.phone_number)
    clean_otp = payload.otp_code.strip()
    if not clean_phone or not re.fullmatch(r"\d{6}", clean_otp):
        raise HTTPException(status_code=400, detail="Invalid phone number or verification code.")
    if len(payload.new_password.encode("utf-8")) > 1024:
        raise HTTPException(status_code=400, detail="Password must be 1024 bytes or fewer.")
    if not auth.record_rate_limit_attempt(
        db, "password-reset-phone", clean_phone, 5, 3600
    ):
        raise HTTPException(status_code=429, detail="Too many password reset attempts. Try again later.")

    valid, err_msg = verify_and_burn_otp(db, clean_phone, clean_otp)
    if not valid:
        raise HTTPException(status_code=400, detail=err_msg)

    user = db.query(models.User).filter_by(phone_number=clean_phone).first()
    if not user or user.role not in {"student", "trainer"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired verification code. Please request a new code.",
        )

    user.hashed_password = auth.get_password_hash(payload.new_password)
    db.commit()
    return {"message": "Password reset successfully. Sign in with your new password."}


@app.post("/auth/request-recovery-phone-otp")
def request_recovery_phone_otp(
    payload: OTPRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    if current_user.role not in {"student", "trainer"}:
        raise HTTPException(status_code=403, detail="Only student and trainer accounts can set a recovery number.")

    client_ip = request.client.host if request.client else "unknown"
    if not auth.record_rate_limit_attempt(db, "recovery-phone-ip", client_ip, 10, 3600):
        raise HTTPException(status_code=429, detail="Too many recovery number requests. Try again later.")

    clean_phone = normalize_phone(payload.phone_number)
    if not clean_phone:
        raise HTTPException(status_code=400, detail="Please enter a valid 10-digit Indian phone number.")
    existing_user = db.query(models.User).filter_by(phone_number=clean_phone).first()
    if existing_user and existing_user.id != current_user.id:
        raise HTTPException(status_code=409, detail="This phone number is already linked to another account.")
    if not settings.DEV_MODE and not is_whatsapp_delivery_configured():
        raise HTTPException(
            status_code=503,
            detail="WhatsApp OTP delivery is not configured. Set the required WhatsApp environment variables.",
        )

    allowed, err_msg = check_rate_limit(db, clean_phone)
    if not allowed:
        raise HTTPException(status_code=429, detail=err_msg)

    otp_code = generate_secure_otp()
    save_otp(db, clean_phone, otp_code)
    if not settings.DEV_MODE:
        try:
            send_whatsapp_otp(clean_phone, otp_code)
        except WhatsAppError as error:
            discard_otp(db, clean_phone)
            raise HTTPException(status_code=502, detail=str(error)) from None

    response = {"message": "A verification code was requested for this recovery number."}
    if settings.DEV_MODE:
        response["development_otp"] = otp_code
    return response


@app.put("/user/recovery-phone")
def set_recovery_phone(
    payload: OTPVerify,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    if current_user.role not in {"student", "trainer"}:
        raise HTTPException(status_code=403, detail="Only student and trainer accounts can set a recovery number.")

    clean_phone = normalize_phone(payload.phone_number)
    clean_otp = payload.otp_code.strip()
    if not clean_phone or not re.fullmatch(r"\d{6}", clean_otp):
        raise HTTPException(status_code=400, detail="Invalid phone number or verification code.")

    valid, err_msg = verify_and_burn_otp(db, clean_phone, clean_otp)
    if not valid:
        raise HTTPException(status_code=400, detail=err_msg)

    existing_user = db.query(models.User).filter_by(phone_number=clean_phone).first()
    if existing_user and existing_user.id != current_user.id:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired verification code. Please request a new code.",
        )

    current_user.phone_number = clean_phone
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This phone number is already linked to another account.",
        ) from None
    return {"message": "Recovery phone number saved.", "phone_number": clean_phone}


@app.get("/user/profile-details")
def get_user_profile(
    db: Session = Depends(get_db), 
    current_user: models.User = Depends(auth.get_current_user)
):
    enrollments = db.query(models.Enrollment).options(
        joinedload(models.Enrollment.course).joinedload(models.Course.trainer)
    ).filter_by(student_id=current_user.id).all()
    enrollments = [
        enrollment for enrollment in enrollments
        if enrollment.course
        and _student_matches_course_subject(current_user, enrollment.course)
    ]
    courses = [e.course.title for e in enrollments if e.course]

    return {
        "name": current_user.name,
        "email": current_user.email,
        "specialization": current_user.specialization,
        "phone_number": current_user.phone_number,
        "role": current_user.role,
        "is_access_granted": current_user.is_access_granted,
        "course_subject": current_user.course_subject,
        "courses": courses
    }


@app.get("/student/dashboard")
def student_dashboard(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    if not current_user.is_access_granted:
        return {
            "alert": "Your registration is awaiting administrative clearance. Please contact support or check back soon."
        }

    enrollments = db.query(models.Enrollment).options(
        joinedload(models.Enrollment.course).joinedload(models.Course.trainer),
        joinedload(models.Enrollment.course).selectinload(
            models.Course.modules
        ).joinedload(
            models.Module.lesson_questions
        ).joinedload(
            models.LessonQuestion.student
        ),
        selectinload(models.Enrollment.progress_list),
    ).filter_by(student_id=current_user.id).all()
    enrollments = [
        enrollment for enrollment in enrollments
        if enrollment.course
        and _student_matches_course_subject(current_user, enrollment.course)
    ]
    courses_data = []

    now = datetime.datetime.now(timezone.utc)

    for enr in enrollments:
        modules = enr.course.modules
        progress_by_module = {
            progress.module_id: progress for progress in enr.progress_list
        }
        modules_list = []
        completed_count = 0

        for idx, mod in enumerate(modules):
            prog = progress_by_module.get(mod.id)

            is_video_done = prog.is_video_completed if prog else False
            status_val = prog.assignment_status if prog else "not_started"
            feedback = prog.trainer_feedback if prog else ""

            if prog and prog.is_video_completed and prog.assignment_completed:
                completed_count += 1

            is_unlocked, lock_reason = _module_unlock_status(
                db, enr, modules, idx, now, progress_by_module
            )

            modules_list.append({
                "module_id": mod.id,
                "order_index": mod.order_index,
                "title": mod.title,
                "video_url": mod.video_url if is_unlocked else None,
                "assignment_prompt": mod.assignment_prompt or "",
                "assignment_link": (
                    get_project_assignment_link(mod.order_index, enr.project_index)
                    if is_unlocked
                    else None
                ) or (
                    mod.assignment_link if is_unlocked else None
                ),
                "is_unlocked": is_unlocked,
                "lock_reason": lock_reason,
                "is_video_completed": is_video_done,
                "assignment_completed": prog.assignment_completed if prog else False,
                "assignment_status": status_val,
                "submission_notes": prog.submission_notes if prog else "",
                "trainer_feedback": feedback,
                "review_available": bool(
                    prog and (
                        prog.assignment_status == "approved"
                        or (prog.assignment_completed and prog.assignment_completed_at)
                    )
                ),
                "questions": [
                    _lesson_question_payload(question)
                    for question in mod.lesson_questions
                ],
            })

        # courses_data.append({
        #     "enrollment_id": enr.id,
        #     "course_title": enr.course.title if enr.course else "Course",
        #     "trainer_name": enr.course.trainer.name if enr.course and enr.course.trainer else "Trainer",
        #     "completed_slots": completed_count,
        #     "modules": modules_list
        # })

    return {
        "course_subject": current_user.course_subject,
        "courses": courses_data,
    }


def _module_unlock_status(
    db: Session,
    enrollment: models.Enrollment,
    modules: list[models.Module],
    module_index: int,
    now: datetime.datetime,
    progress_by_module: dict[str, models.StudentProgress] | None = None,
) -> tuple[bool, str]:
    if module_index == 0:
        return True, ""

    previous_module = modules[module_index - 1]
    if progress_by_module is None:
        previous_progress = db.query(models.StudentProgress).filter_by(
            enrollment_id=enrollment.id,
            module_id=previous_module.id
        ).first()
    else:
        previous_progress = progress_by_module.get(previous_module.id)
    if not previous_progress or not previous_progress.is_video_completed:
        return False, f"Complete Lesson {previous_module.order_index} first."
    if not previous_progress.assignment_completed:
        return False, f"Complete the task for Lesson {previous_module.order_index} first."
    if not previous_progress.assignment_completed_at:
        return False, f"Lesson {previous_module.order_index} completion time is missing."

    completed_at = previous_progress.assignment_completed_at
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    target_unlock = completed_at + timedelta(hours=24)
    if now < target_unlock:
        remaining = target_unlock - now
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        return False, (
            f"Unlocks in {hours}h {minutes}m "
            "(24h cooldown after task completion)."
        )
    return True, ""


def _get_unlocked_student_module(
    db: Session,
    current_user: models.User,
    enrollment_id: str,
    module_id: str,
) -> tuple[models.Enrollment, models.Module]:
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Student access is required.")
    if not current_user.is_access_granted:
        raise HTTPException(status_code=403, detail="Your account is awaiting clearance.")

    enrollment = db.query(models.Enrollment).filter_by(
        id=enrollment_id,
        student_id=current_user.id
    ).first()
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found.")
    if enrollment.course and not _student_matches_course_subject(
        current_user,
        enrollment.course,
    ):
        raise HTTPException(
            status_code=403,
            detail="This course does not match your selected course subject.",
        )

    modules = db.query(models.Module).filter_by(
        course_id=enrollment.course_id
    ).order_by(models.Module.order_index).all()
    module_index = next(
        (index for index, module in enumerate(modules) if module.id == module_id),
        None
    )
    if module_index is None:
        raise HTTPException(status_code=404, detail="Lesson not found.")

    is_unlocked, _ = _module_unlock_status(
        db, enrollment, modules, module_index, datetime.datetime.now(timezone.utc)
    )
    if not is_unlocked:
        raise HTTPException(status_code=403, detail="This lesson is still locked.")
    return enrollment, modules[module_index]


@app.post(
    "/student/enrollments/{enrollment_id}/modules/{module_id}/questions",
    status_code=201,
)
def create_lesson_question(
    enrollment_id: str,
    module_id: str,
    payload: schemas.LessonQuestionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("student")),
):
    _, module = _get_unlocked_student_module(
        db,
        current_user,
        enrollment_id,
        module_id,
    )
    question_text = payload.question.strip()
    if not question_text:
        raise HTTPException(status_code=422, detail="Question cannot be empty.")

    question = models.LessonQuestion(
        id=str(uuid.uuid4()),
        module_id=module.id,
        student_id=current_user.id,
        question=question_text,
    )
    db.add(question)
    db.commit()
    db.refresh(question)
    return _lesson_question_payload(question)


@app.post("/trainer/lesson-questions/{question_id}/reply")
def reply_to_lesson_question(
    question_id: str,
    payload: schemas.LessonQuestionReply,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    question = db.query(models.LessonQuestion).options(
        joinedload(models.LessonQuestion.student),
        joinedload(models.LessonQuestion.module).joinedload(models.Module.course),
    ).filter_by(id=question_id).first()
    if not question:
        raise HTTPException(status_code=404, detail="Lesson question not found.")
    if not question.module.course or question.module.course.trainer_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="You can only reply to questions in your own courses.",
        )

    reply_text = payload.reply.strip()
    if not reply_text:
        raise HTTPException(status_code=422, detail="Reply cannot be empty.")

    question.trainer_reply = reply_text
    question.replied_at = datetime.datetime.now(timezone.utc)
    db.commit()
    db.refresh(question)
    return _lesson_question_payload(question)


@app.post("/integrations/assignment-completed")
async def assignment_completion_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    """Accept a completion event signed over timestamp + '.' + exact JSON request body.

    The provider sends X-Assignment-Timestamp and a hex SHA-256
    X-Assignment-Signature using ASSIGNMENT_WEBHOOK_SECRET. The JSON body
    contains enrollment_id, module_id, and completed: true.
    """
    secret = settings.ASSIGNMENT_WEBHOOK_SECRET
    if not secret:
        raise HTTPException(status_code=503, detail="Assignment webhook is not configured.")

    timestamp = request.headers.get("X-Assignment-Timestamp", "")
    signature = request.headers.get("X-Assignment-Signature", "")
    try:
        timestamp_value = int(timestamp)
    except ValueError as error:
        raise HTTPException(status_code=401, detail="Invalid webhook signature.") from error
    if abs(int(time.time()) - timestamp_value) > 300:
        raise HTTPException(status_code=401, detail="Webhook timestamp has expired.")

    body = await request.body()
    signed_payload = timestamp.encode() + b"." + body
    expected_signature = hmac.new(
        secret.encode(),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    try:
        payload = AssignmentCompletionWebhook.model_validate_json(body)
    except ValidationError as error:
        raise HTTPException(status_code=400, detail="Invalid assignment completion payload.") from error
    if not payload.completed:
        raise HTTPException(status_code=400, detail="Completion event must be marked completed.")

    enrollment = db.query(models.Enrollment).filter_by(id=payload.enrollment_id).first()
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found.")
    module = db.query(models.Module).filter_by(
        id=payload.module_id,
        course_id=enrollment.course_id,
    ).first()
    if not module:
        raise HTTPException(status_code=404, detail="Lesson not found in this enrollment.")

    progress = db.query(models.StudentProgress).filter_by(
        enrollment_id=enrollment.id,
        module_id=module.id,
    ).first()
    if not progress:
        raise HTTPException(
            status_code=409,
            detail="Complete the lesson video before marking its task complete.",
        )
    if not progress.is_video_completed:
        raise HTTPException(
            status_code=409,
            detail="Complete the lesson video before marking its task complete.",
        )
    if not progress.assignment_completed:
        progress.assignment_completed = True
        progress.assignment_completed_at = datetime.datetime.now(timezone.utc)
    db.commit()
    return {"message": "Assignment completion recorded."}


def _review_token(progress: models.StudentProgress) -> str:
    completed_at = progress.assignment_completed_at
    if not completed_at:
        raise ValueError("Completed assignment timestamp is required for review token.")
    token_payload = f"{progress.id}:{completed_at.isoformat()}".encode("utf-8")
    return hmac.new(
        settings.JWT_SECRET_KEY.encode("utf-8"),
        token_payload,
        hashlib.sha256,
    ).hexdigest()


def _get_review_progress(
    db: Session,
    progress_id: str,
    token: str,
) -> models.StudentProgress:
    progress = db.query(models.StudentProgress).options(
        joinedload(models.StudentProgress.enrollment).joinedload(models.Enrollment.course),
        joinedload(models.StudentProgress.module),
    ).filter_by(id=progress_id).first()
    if (
        not progress
        or not progress.assignment_completed
        or not progress.assignment_completed_at
        or not hmac.compare_digest(_review_token(progress), token)
    ):
        raise HTTPException(status_code=404, detail="Task review link is invalid or expired.")
    return progress


def _review_answers_are_valid(answers: dict[str, str]) -> bool:
    expected_questions = {question["id"]: question for question in REVIEW_QUESTIONS}
    if set(answers) != set(expected_questions):
        return False
    return all(
        any(value == option_value for option_value, _ in expected_questions[question_id]["options"])
        for question_id, value in answers.items()
    )


@app.get("/student/enrollments/{enrollment_id}/modules/{module_id}/review-qr")
def task_review_qr(
    enrollment_id: str,
    module_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("student")),
):
    progress = db.query(models.StudentProgress).join(
        models.Enrollment,
    ).filter(
        models.StudentProgress.enrollment_id == enrollment_id,
        models.StudentProgress.module_id == module_id,
        models.Enrollment.student_id == current_user.id,
    ).first()
    if not progress:
        raise HTTPException(status_code=404, detail="Lesson progress not found.")
    if progress.assignment_status == "approved" and (
        not progress.assignment_completed or not progress.assignment_completed_at
    ):
        progress.assignment_completed = True
        progress.assignment_completed_at = (
            progress.assignment_completed_at
            or progress.evaluated_at
            or datetime.datetime.now(timezone.utc)
        )
        db.commit()
    if not progress.assignment_completed or not progress.assignment_completed_at:
        raise HTTPException(
            status_code=409,
            detail="Complete the task before opening its review form.",
        )

    base_url = settings.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    review_url = (
        f"{base_url}/task-review/{progress.id}/{_review_token(progress)}"
    )
    qr_image = qrcode.make(review_url)
    image_buffer = io.BytesIO()
    qr_image.save(image_buffer, format="PNG")
    return {
        "enrollment_id": enrollment_id,
        "module_id": module_id,
        "review_url": review_url,
        "qr_code": base64.b64encode(image_buffer.getvalue()).decode("ascii"),
    }


@app.get("/task-review/{progress_id}/{token}")
def task_review_page(
    progress_id: str,
    token: str,
    db: Session = Depends(get_db),
):
    _get_review_progress(db, progress_id, token)
    return FileResponse("templates/task_review.html")


@app.get("/api/task-review/{progress_id}/{token}")
def task_review_details(
    progress_id: str,
    token: str,
    db: Session = Depends(get_db),
):
    progress = _get_review_progress(db, progress_id, token)
    return {
        "course_title": progress.enrollment.course.title,
        "lesson_title": progress.module.title,
        "questions": [
            {
                "id": question["id"],
                "prompt": question["prompt"],
                "options": [
                    {"value": value, "label": label}
                    for value, label in question["options"]
                ],
            }
            for question in REVIEW_QUESTIONS
        ],
        "answers": json.loads(progress.review_answers) if progress.review_answers else {},
        "generated_review": progress.generated_review,
        "redirect_url": settings.REVIEW_REDIRECT_URL,
    }


@app.post("/api/task-review/{progress_id}/{token}/generate")
def generate_task_review(
    progress_id: str,
    token: str,
    payload: schemas.TaskReviewAnswers,
    db: Session = Depends(get_db),
):
    progress = _get_review_progress(db, progress_id, token)
    if not _review_answers_are_valid(payload.answers):
        raise HTTPException(
            status_code=422,
            detail="Select one valid answer for every review question.",
        )

    answers_json = json.dumps(payload.answers, sort_keys=True)
    if progress.generated_review:
        if progress.review_answers != answers_json:
            raise HTTPException(
                status_code=409,
                detail="A review has already been generated for this task.",
            )
        return {
            "review": progress.generated_review,
            "redirect_url": settings.REVIEW_REDIRECT_URL,
        }

    progress.review_answers = answers_json
    db.commit()

    if not settings.OPENAI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="AI review generation is not configured. Add OPENAI_API_KEY to the server environment.",
        )

    redirect_parts = urlsplit(settings.REVIEW_REDIRECT_URL)
    if redirect_parts.scheme != "https" or not redirect_parts.netloc:
        raise HTTPException(
            status_code=503,
            detail="The review destination must be configured as a valid HTTPS URL.",
        )

    try:
        review_text = generate_review(
            progress.enrollment.course.title,
            progress.module.title,
            payload.answers,
            settings.OPENAI_API_KEY,
            settings.OPENAI_MODEL,
        )
    except ReviewProviderError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    progress.generated_review = review_text
    progress.review_generated_at = datetime.datetime.now(timezone.utc)
    db.commit()
    return {
        "review": review_text,
        "redirect_url": settings.REVIEW_REDIRECT_URL,
    }


@app.post("/student/complete-video/{enrollment_id}/{module_id}")
def mark_video_completed(
    enrollment_id: str, 
    module_id: str, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    _get_unlocked_student_module(db, current_user, enrollment_id, module_id)
    prog = db.query(models.StudentProgress).filter_by(
        enrollment_id=enrollment_id,
        module_id=module_id
    ).first()

    if not prog:
        prog = models.StudentProgress(
            id=str(uuid.uuid4()),
            enrollment_id=enrollment_id,
            module_id=module_id,
            is_video_completed=True,
            video_completed_at=datetime.datetime.now(timezone.utc),
            assignment_status="not_started"
        )
        db.add(prog)
    else:
        prog.is_video_completed = True
        prog.video_completed_at = datetime.datetime.now(timezone.utc)

    db.commit()
    return {"message": "Video completion verified."}


@app.post("/student/submit-assignment")
def submit_assignment(
    enrollment_id: str = Form(...),
    module_id: str = Form(...),
    submission_text: str = Form(..., min_length=1, max_length=4000),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    _get_unlocked_student_module(db, current_user, enrollment_id, module_id)
    prog = db.query(models.StudentProgress).filter_by(
        enrollment_id=enrollment_id,
        module_id=module_id
    ).first()

    if not prog or not prog.is_video_completed:
        raise HTTPException(status_code=400, detail="You must complete 95% of the lecture video first.")
    if prog.assignment_status in {"submitted", "approved"}:
        raise HTTPException(status_code=409, detail="This assignment cannot be submitted again in its current status.")
    submission_text = submission_text.strip()
    if not submission_text:
        raise HTTPException(status_code=422, detail="Provide a description or link to your completed work.")

    prog.assignment_status = "submitted"
    prog.submission_notes = submission_text
    prog.submitted_at = datetime.datetime.now(timezone.utc)
    db.commit()
    return {"message": "Assignment successfully submitted for trainer evaluation."}


@app.get("/trainer/dashboard")
def trainer_dashboard(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer"))
):
    courses = db.query(models.Course).options(
        selectinload(models.Course.modules).joinedload(
            models.Module.lesson_questions
        ).joinedload(
            models.LessonQuestion.student
        ),
        selectinload(models.Course.enrollments).selectinload(models.Enrollment.student),
        selectinload(models.Course.enrollments).selectinload(models.Enrollment.progress_list),
    ).filter_by(trainer_id=current_user.id).all()
    courses_payload = []

    for c in courses:
        enrollments = [
            enrollment for enrollment in c.enrollments
            if enrollment.student and enrollment.student.course_subject
            and normalize_course_subject(current_user.specialization)
                == normalize_course_subject(enrollment.student.course_subject)
        ]
        modules = c.modules
        student_records = []
        for e in enrollments:
            progress_by_module = {
                progress.module_id: progress for progress in e.progress_list
            }
            completed_count = sum(
                1 for module in modules
                if (
                    progress_by_module.get(module.id)
                    and progress_by_module[module.id].is_video_completed
                    and progress_by_module[module.id].assignment_completed
                )
            )
            module_progress = []
            for module in modules:
                progress = progress_by_module.get(module.id)
                module_progress.append({
                    "module_id": module.id,
                    "title": module.title,
                    "assignment_status": progress.assignment_status if progress else "not_started",
                    "is_video_completed": progress.is_video_completed if progress else False,
                    "assignment_completed": progress.assignment_completed if progress else False,
                    "assignment_link": (
                        get_project_assignment_link(module.order_index, e.project_index)
                    ) or module.assignment_link,
                })
            student_records.append({
                "student_id": e.student.id if e.student else None,
                "student_name": e.student.name if e.student else "Student",
                "student_email": e.student.email if e.student else "N/A",
                "course_title": c.title,
                "enrolled_at": e.enrolled_at.isoformat() if e.enrolled_at else None,
                "completed_slots": completed_count,
                "total_slots": len(modules),
                "progress_percent": round(completed_count * 100 / len(modules)) if modules else 0,
                "modules": module_progress,
            })

        courses_payload.append({
            "id": c.id,
            "title": c.title,
            "description": c.description,
            "total_enrolled": len(enrollments),
            "modules": [
                {
                    "module_id": module.id,
                    "order_index": module.order_index,
                    "title": module.title,
                    "video_url": module.video_url,
                    "assignment_prompt": module.assignment_prompt,
                    "assignment_link": module.assignment_link,
                    "questions": [
                        _lesson_question_payload(question)
                        for question in module.lesson_questions
                    ],
                }
                for module in modules
            ],
            "students": student_records
        })

    return {"courses": courses_payload}


@app.post("/trainer/courses", status_code=201)
def create_trainer_course(
    payload: schemas.CourseCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    order_indexes = [module.order_index for module in payload.modules]
    if len(order_indexes) != len(set(order_indexes)):
        raise HTTPException(status_code=422, detail="Lesson order numbers must be unique.")

    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Course title cannot be empty.")

    course = models.Course(
        id=str(uuid.uuid4()),
        title=title,
        description=payload.description,
        trainer_id=current_user.id,
    )
    db.add(course)
    for module_payload in sorted(payload.modules, key=lambda module: module.order_index):
        module_title = module_payload.title.strip()
        video_url = module_payload.video_url.strip()
        assignment_prompt = module_payload.assignment_prompt.strip()
        if not module_title or not video_url or not assignment_prompt:
            raise HTTPException(
                status_code=422,
                detail="Lesson title, video URL, and assignment prompt cannot be empty.",
            )
        db.add(models.Module(
            id=str(uuid.uuid4()),
            course_id=course.id,
            order_index=module_payload.order_index,
            title=module_title,
            video_url=video_url,
            assignment_prompt=assignment_prompt,
            assignment_link=module_payload.assignment_link.strip()
                if module_payload.assignment_link else None,
        ))
    # A new course must NOT auto-enroll existing students.
    # Trainers assign it explicitly via /trainer/assign-course.
    db.commit()
    return {
        "course_id": course.id,
        "title": course.title,
        "module_count": len(payload.modules),
    }


@app.post("/trainer/courses/{course_id}/modules", status_code=201)
def add_trainer_course_module(
    course_id: str,
    payload: schemas.ModuleCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    course = db.query(models.Course).filter_by(
        id=course_id,
        trainer_id=current_user.id,
    ).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")

    last_module = db.query(models.Module).filter_by(
        course_id=course.id
    ).order_by(models.Module.order_index.desc()).first()
    next_order_index = last_module.order_index + 1 if last_module else 1
    if payload.order_index != next_order_index:
        raise HTTPException(
            status_code=422,
            detail=f"New lesson order must be {next_order_index}.",
        )

    title = payload.title.strip()
    video_url = payload.video_url.strip()
    assignment_prompt = payload.assignment_prompt.strip()
    if not title or not video_url or not assignment_prompt:
        raise HTTPException(
            status_code=422,
            detail="Lesson title, video URL, and assignment prompt cannot be empty.",
        )

    module = models.Module(
        id=str(uuid.uuid4()),
        course_id=course.id,
        order_index=next_order_index,
        title=title,
        video_url=video_url,
        assignment_prompt=assignment_prompt,
        assignment_link=payload.assignment_link.strip() if payload.assignment_link else None,
    )
    db.add(module)
    db.commit()
    return {
        "module_id": module.id,
        "course_id": course.id,
        "order_index": module.order_index,
        "title": module.title,
    }
class CourseAssignPayload(BaseModel):
    student_id: str
    course_id: str

# 1. Fetch all students currently under the logged-in trainer
@app.get("/trainer/my-students")
def get_trainer_students(current_user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["trainer", "admin"]:
        raise HTTPException(status_code=403, detail="Access denied")

    # If admin, fetch all courses; if trainer, fetch only courses owned by this trainer
    if current_user.role == "admin":
        trainer_courses = db.query(models.Course).all()
    else:
        trainer_courses = db.query(models.Course).filter(models.Course.trainer_id == current_user.id).all()
    
    trainer_course_ids = [c.id for c in trainer_courses]

    # Find all enrollments under these courses
    enrollments = db.query(models.Enrollment).filter(
        models.Enrollment.course_id.in_(trainer_course_ids)
    ).all()

    # Group students and collect their enrolled courses
    students_dict = {}
    for enr in enrollments:
        student = db.query(models.User).filter(models.User.id == enr.student_id).first()
        if not student:
            continue
        
        if student.id not in students_dict:
            students_dict[student.id] = {
                "id": student.id,
                "name": student.name,
                "email": student.email,
                "phone_number": student.phone_number,
                "enrolled_course_ids": set(),
                "enrolled_courses": []
            }
        course = next((c for c in trainer_courses if c.id == enr.course_id), None)
        if course:
            students_dict[student.id]["enrolled_course_ids"].add(course.id)
            students_dict[student.id]["enrolled_courses"].append({
                "id": course.id,
                "title": course.title
            })

    # Prepare response: For each student, include eligible courses they are NOT yet enrolled in
    result = []
    for s_id, s_data in students_dict.items():
        available_to_assign = [
            {"id": c.id, "title": c.title}
            for c in trainer_courses
            if c.id not in s_data["enrolled_course_ids"]
        ]
        result.append({
            "id": s_data["id"],
            "name": s_data["name"],
            "email": s_data["email"],
            "phone_number": s_data["phone_number"],
            "enrolled_courses": s_data["enrolled_courses"],
            "available_to_assign": available_to_assign
        })

    return {
        "students": result,
        "trainer_courses": [{"id": c.id, "title": c.title} for c in trainer_courses]
    }

@app.post("/trainer/assign-course")
def assign_course_to_student(
    payload: CourseAssignPayload,
    current_user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    # Verify target course exists
    target_course = db.query(models.Course).filter(models.Course.id == payload.course_id).first()
    if not target_course:
        raise HTTPException(status_code=404, detail="Target course not found")

    # ROLE VERIFICATION:
    if current_user.role == "trainer":
        # Rule 1: Trainer must own the course they want to assign
        if target_course.trainer_id != current_user.id:
            raise HTTPException(status_code=403, detail="You can only assign courses that you instruct.")

        # Rule 2: Student must already be enrolled in at least one course taught by this trainer
        trainer_course_ids = [
            c.id for c in db.query(models.Course).filter(models.Course.trainer_id == current_user.id).all()
        ]
        is_student_under_trainer = db.query(models.Enrollment).filter(
            models.Enrollment.student_id == payload.student_id,
            models.Enrollment.course_id.in_(trainer_course_ids)
        ).first()

        if not is_student_under_trainer:
            raise HTTPException(status_code=403, detail="You can only assign additional courses to students already enrolled under you.")

    elif current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Unauthorized")
    # Note: Admin bypasses the checks above and has universal authority to assign any course to any student.

    # Check if student is already enrolled in this exact course
    existing_enr = db.query(models.Enrollment).filter(
        models.Enrollment.student_id == payload.student_id,
        models.Enrollment.course_id == payload.course_id
    ).first()
    if existing_enr:
        raise HTTPException(status_code=400, detail="Student is already enrolled in this course.")

    # Create the new enrollment
    new_enrollment = models.Enrollment(
        student_id=payload.student_id,
        course_id=payload.course_id
    )
    db.add(new_enrollment)
    db.flush()

    # Initialize progress for the first module of the newly assigned course
    first_module = db.query(models.Module).filter(
        models.Module.course_id == payload.course_id
    ).order_index if hasattr(models.Module, 'order_index') else None
    
    first_mod = db.query(models.Module).filter(
        models.Module.course_id == payload.course_id
    ).order_by(models.Module.order_index.asc()).first()

    if first_mod:
        new_prog = models.Progress(
            enrollment_id=new_enrollment.id,
            module_id=first_mod.id,
            is_video_completed=False,
            assignment_status="not_submitted"
        )
        db.add(new_prog)

    db.commit()
    return {"status": "success", "message": f"Successfully assigned '{target_course.title}' to the student."}


@app.get("/trainer/pending-assignments")
def pending_assignments(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer"))
):
    pending = db.query(models.StudentProgress).join(models.Enrollment).join(models.Course).filter(
        models.Course.trainer_id == current_user.id,
        models.StudentProgress.assignment_status == "submitted"
    ).all()
    pending = [
        progress for progress in pending
        if progress.enrollment and progress.enrollment.student
        and progress.enrollment.course
        and _student_matches_course_subject(
            progress.enrollment.student,
            progress.enrollment.course,
        )
    ]

    return [
        {
            "progress_id": p.id,
            "student_name": p.enrollment.student.name if p.enrollment.student else "Candidate",
            "student_email": p.enrollment.student.email if p.enrollment.student else "N/A",
            "submission_notes": p.submission_notes or "",
            "submitted_at": p.submitted_at.isoformat() if p.submitted_at else None,
            "course_title": p.enrollment.course.title if p.enrollment.course else "Course",
            "module_title": p.module.title if p.module else "Module",
            "assignment_link": (
                (
                    get_project_assignment_link(
                        p.module.order_index,
                        p.enrollment.project_index,
                    )
                    if p.module and p.enrollment else None
                )
                or (p.module.assignment_link if p.module else None)
            ),
            "assignment_prompt": p.module.assignment_prompt if p.module else "",
        }
        for p in pending
    ]


@app.post("/trainer/assignments/{progress_id}/open-submission")
def trainer_open_assignment_submission(
    progress_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    progress = db.query(models.StudentProgress).filter_by(id=progress_id).first()
    if not progress:
        raise HTTPException(status_code=404, detail="Submission record not found.")
    course = progress.enrollment.course if progress.enrollment else None
    if not course:
        raise HTTPException(status_code=404, detail="Assignment course not found.")
    if course.trainer_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="You cannot open submissions for another trainer's course.",
        )
    if progress.assignment_status != "submitted":
        raise HTTPException(status_code=409, detail="This assignment has not been submitted.")

    match = re.search(r"https?://[^\s<>\"']+", progress.submission_notes or "", re.IGNORECASE)
    if not match:
        raise HTTPException(
            status_code=409,
            detail="The student has not provided a clickable work link.",
        )
    submitted_url = match.group().rstrip(".,;:!?)")
    url_parts = urlsplit(submitted_url)
    if (
        url_parts.scheme.lower() not in {"http", "https"}
        or not url_parts.hostname
        or url_parts.username
        or url_parts.password
    ):
        raise HTTPException(status_code=409, detail="The submitted work link is invalid.")

    if not progress.assignment_completed:
        progress.assignment_completed = True
        progress.assignment_completed_at = datetime.datetime.now(timezone.utc)
        db.commit()
    return {"message": "Submission opened. The student task review QR code is now available."}


@app.post("/trainer/request-student-deletion", status_code=201)
def trainer_request_student_deletion(
    payload: schemas.TrainerDeletionRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="Provide a reason for the deletion request.")

    student = db.query(models.User).filter_by(
        id=payload.student_id,
        role="student",
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")
    if (
        student.course_subject
        and normalize_course_subject(current_user.specialization)
        != student.course_subject
    ):
        raise HTTPException(
            status_code=403,
            detail="You can only request deletion for students in your course subject.",
        )

    enrollment = db.query(models.Enrollment).join(models.Course).filter(
        models.Enrollment.student_id == payload.student_id,
        models.Course.trainer_id == current_user.id,
    ).first()
    if not enrollment:
        raise HTTPException(
            status_code=404,
            detail="Student is not enrolled in any of your courses.",
        )

    existing_request = db.query(models.DeletionRequest).filter_by(
        student_id=payload.student_id,
        trainer_id=current_user.id,
    ).first()
    if existing_request:
        if existing_request.is_approved:
            raise HTTPException(
                status_code=409,
                detail="This student deletion is approved. Delete the student from your dashboard.",
            )
        raise HTTPException(
            status_code=409,
            detail="A deletion request for this student is already awaiting admin approval.",
        )

    deletion_request = models.DeletionRequest(
        id=str(uuid.uuid4()),
        student_id=payload.student_id,
        trainer_id=current_user.id,
        reason=reason,
        is_approved=False,
    )
    db.add(deletion_request)
    db.commit()
    return {
        "request_id": deletion_request.id,
        "message": "Deletion request sent to an administrator for approval.",
    }


@app.get("/trainer/deletion-requests")
def trainer_deletion_requests(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    requests = db.query(models.DeletionRequest).filter_by(
        trainer_id=current_user.id,
    ).all()
    return [
        {
            "student_id": request.student_id,
            "reason": request.reason,
            "status": "approved" if request.is_approved else "pending",
        }
        for request in requests
    ]


@app.post("/trainer/delete-approved-student/{student_id}")
def trainer_delete_approved_student(
    student_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    deletion_request = db.query(models.DeletionRequest).filter_by(
        student_id=student_id,
        trainer_id=current_user.id,
        is_approved=True,
    ).first()
    if not deletion_request:
        raise HTTPException(
            status_code=403,
            detail="An administrator must approve this student's deletion first.",
        )

    student = db.query(models.User).filter_by(
        id=student_id,
        role="student",
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")

    enrollment = db.query(models.Enrollment).join(models.Course).filter(
        models.Enrollment.student_id == student_id,
        models.Course.trainer_id == current_user.id,
    ).first()
    if not enrollment:
        raise HTTPException(
            status_code=404,
            detail="Student is no longer enrolled in any of your courses.",
        )

    student_name = student.name
    db.query(models.DeletionRequest).filter_by(
        student_id=student_id,
    ).delete(synchronize_session=False)
    db.delete(student)
    db.commit()
    return {"message": f"Student account for {student_name} was deleted."}


@app.post("/trainer/evaluate-assignment")
def evaluate_assignment(
    payload: AssignmentEvaluation,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer"))
):
    prog = db.query(models.StudentProgress).filter_by(id=payload.progress_id).first()
    if not prog:
        raise HTTPException(status_code=404, detail="Submission record not found.")
    course = prog.enrollment.course if prog.enrollment else None
    if not course:
        raise HTTPException(status_code=404, detail="Assignment course not found.")
    if course.trainer_id != current_user.id:
        raise HTTPException(status_code=403, detail="You cannot evaluate assignments for another trainer's course.")
    if prog.assignment_status != "submitted":
        raise HTTPException(status_code=409, detail="This assignment has already been evaluated.")

    prog.is_approved = payload.is_approved
    prog.assignment_status = "approved" if payload.is_approved else "rejected"
    prog.trainer_feedback = payload.feedback
    evaluated_at = datetime.datetime.now(timezone.utc)
    prog.evaluated_at = evaluated_at
    if payload.is_approved:
        prog.assignment_completed = True
        prog.assignment_completed_at = evaluated_at
    else:
        prog.assignment_completed = False
        prog.assignment_completed_at = None
    db.commit()
    return {"message": "Evaluation recorded. 24-hour unlock cooldown has started." if payload.is_approved else "Assignment rejected."}


@app.get("/admin/dashboard")
def admin_dashboard(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("admin"))
):
    total_trainers = db.query(models.User).filter_by(role="trainer").count()
    total_students = db.query(models.User).filter_by(role="student").count()
    pending_approvals = db.query(models.User).filter_by(role="student", is_access_granted=False).count()
    pending_deletions = db.query(models.DeletionRequest).filter_by(is_approved=False).count()

    return {
        "total_trainers": total_trainers,
        "total_students": total_students,
        "pending_student_access_approvals": pending_approvals,
        "pending_deletion_requests": pending_deletions
    }


@app.get("/admin/students")
def admin_student_list(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("admin")),
):
    students = db.query(models.User).options(
        selectinload(models.User.enrollments).selectinload(models.Enrollment.course).selectinload(models.Course.modules),
        selectinload(models.User.enrollments).selectinload(models.Enrollment.progress_list),
    ).filter_by(role="student").order_by(
        models.User.name,
        models.User.email,
    ).all()
    student_rows = []

    for student in students:
        enrollments = sorted(
            student.enrollments,
            key=lambda enrollment: enrollment.enrolled_at or datetime.datetime.min,
        )
        if not enrollments:
            student_rows.append({
                "student_id": student.id,
                "student_name": student.name,
                "student_email": student.email,
                "phone_number": student.phone_number,
                "course_title": "Not enrolled",
                "enrolled_at": None,
                "completed_slots": 0,
                "total_slots": 0,
                "modules": [],
            })
            continue

        for enrollment in enrollments:
            progress_by_module = {
                progress.module_id: progress for progress in enrollment.progress_list
            }
            module_progress = []
            for module in enrollment.course.modules if enrollment.course else []:
                progress = progress_by_module.get(module.id)
                video_completed = bool(progress and progress.is_video_completed)
                assignment_completed = bool(progress and progress.assignment_completed)
                module_progress.append({
                    "module_id": module.id,
                    "is_video_completed": video_completed,
                    "assignment_completed": assignment_completed,
                })
            completed_count = sum(
                1 for module in module_progress
                if module["is_video_completed"] and module["assignment_completed"]
            )
            student_rows.append({
                "student_id": student.id,
                "student_name": student.name,
                "student_email": student.email,
                "phone_number": student.phone_number,
                "course_title": enrollment.course.title if enrollment.course else "Course",
                "enrolled_at": enrollment.enrolled_at.isoformat() if enrollment.enrolled_at else None,
                "completed_slots": completed_count,
                "total_slots": len(module_progress),
                "modules": module_progress,
            })
    return {"students": student_rows}


@app.post("/admin/trainers", status_code=201)
def admin_create_trainer(
    payload: schemas.TrainerCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("admin")),
):
    name = payload.name.strip()
    specialization = payload.specialization.strip()
    email = str(payload.email).strip().lower()
    clean_phone = normalize_phone(payload.phone_number)
    selected_specialization = normalize_course_subject(specialization)
    if not name or not selected_specialization:
        raise HTTPException(
            status_code=422,
            detail="Choose a valid course specialization for the trainer.",
        )
    if not clean_phone:
        raise HTTPException(
            status_code=422,
            detail="Enter a valid 10-digit Indian mobile number for password recovery.",
        )
    if len(payload.password.encode("utf-8")) > 1024:
        raise HTTPException(status_code=422, detail="Password must be 1024 bytes or fewer.")

    existing_user = db.query(models.User).filter(
        (func.lower(models.User.email) == email)
        | (models.User.phone_number == clean_phone)
    ).first()
    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="An account with this email or phone number already exists.",
        )

    trainer = models.User(
        id=str(uuid.uuid4()),
        name=name,
        email=email,
        phone_number=clean_phone,
        specialization=selected_specialization,
        hashed_password=auth.get_password_hash(payload.password),
        role="trainer",
        is_access_granted=True,
    )
    db.add(trainer)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="An account with this email or phone number already exists.",
        ) from None
    return {
        "trainer_id": trainer.id,
        "name": trainer.name,
        "email": trainer.email,
        "specialization": trainer.specialization,
        "message": "Trainer account created. They can now sign in with the provided email and password.",
    }


@app.get("/admin/students-analytics")
def admin_students_analytics(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("admin"))
):
    students = db.query(models.User).filter_by(role="student").all()
    results = []

    for s in students:
        enrollments = db.query(models.Enrollment).filter_by(student_id=s.id).all()
        c_list = []
        for e in enrollments:
            done = db.query(models.StudentProgress).filter_by(enrollment_id=e.id, is_approved=True).count()
            c_list.append({
                "course_title": e.course.title if e.course else "Course",
                "completed_slots": done
            })

        results.append({
            "id": s.id,
            "name": s.name,
            "email": s.email,
            "phone_number": s.phone_number,
            "is_access_granted": s.is_access_granted,
            "courses": c_list
        })

    return results


@app.get("/admin/pending-access-list")
def admin_pending_access_list(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("admin"))
):
    students = db.query(models.User).filter_by(role="student", is_access_granted=False).all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "email": s.email,
            "phone_number": s.phone_number
        }
        for s in students
    ]


@app.post("/admin/grant-access/{student_id}")
def admin_grant_access(
    student_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("admin"))
):
    student = db.query(models.User).filter_by(id=student_id, role="student").first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")

    student.is_access_granted = True
    db.commit()
    return {"message": f"Access clearance granted to {student.name}."}


@app.get("/admin/pending-deletion-list")
def admin_pending_deletions(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("admin"))
):
    requests = db.query(models.DeletionRequest).filter_by(is_approved=False).all()
    return [
        {
            "request_id": r.id,
            "trainer_name": r.trainer.name if r.trainer else "Trainer",
            "trainer_email": r.trainer.email if r.trainer else "N/A",
            "student_name": r.student.name if r.student else "Student",
            "student_email": r.student.email if r.student else "N/A",
            "reason": r.reason,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in requests
    ]


@app.post("/admin/process-deletion")
def admin_process_deletion(
    payload: DeletionProcess,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("admin"))
):
    req = db.query(models.DeletionRequest).filter_by(id=payload.request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Deletion request not found.")
    if req.is_approved:
        raise HTTPException(status_code=409, detail="This deletion request has already been processed.")

    if payload.approved:
        student = db.query(models.User).filter_by(id=req.student_id).first()
        if not student or student.role != "student":
            raise HTTPException(status_code=404, detail="Requested student no longer exists.")
        req.is_approved = True
        db.commit()
        return {"message": "Deletion approved. The trainer can now delete this student from their dashboard."}
    else:
        db.delete(req)
        db.commit()
        return {"message": "Student deletion request was rejected."}