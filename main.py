import os
import re
import datetime
import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import io
import json
import time
from datetime import timezone, timedelta
from typing import Optional
from urllib.parse import urljoin, urlsplit
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
import database
from config import settings
from database import engine, get_db
from review_service import REVIEW_QUESTIONS, ReviewProviderError, generate_review

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

# Keep this enabled only while trainers and students test every course day.
DAYS_UNLOCKED_FOR_TESTING = True

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

progress_columns = {
    column["name"] for column in inspect(engine).get_columns("progress")
}
for column_name, column_definition in (
    ("assignment_completed_at", "DATETIME"),
    ("review_answers", "TEXT"),
    ("generated_review", "TEXT"),
    ("review_generated_at", "DATETIME"),
    ("day_one_state", "TEXT"),
    ("daily_task_state", "TEXT"),
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

def _ensure_existing_courses_have_twelve_days():
    db = database.SessionLocal()
    try:
        courses = db.query(models.Course).options(
            selectinload(models.Course.modules)
        ).all()
        for course in courses:
            existing_orders = {module.order_index for module in course.modules}
            for order_index in range(1, 13):
                if order_index in existing_orders:
                    continue
                db.add(models.Module(
                    id=str(uuid.uuid4()),
                    course_id=course.id,
                    order_index=order_index,
                    title=f"Day {order_index}",
                    video_url="",
                    assignment_prompt=(
                        "Complete the website checks for each site."
                        if order_index == 1
                        else _daily_task_prompt(order_index)
                    ),
                ))
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _ensure_existing_courses_have_twelve_days()
    yield


app = FastAPI(title="prodigiter LMS Portal", lifespan=lifespan)

DAY_ONE_WEBSITES = (
    {"key": "beyondental", "name": "Beyon Dental", "url": "https://beyondental.in/"},
    {"key": "birchstone", "name": "Birchstone", "url": "https://birchstone.in/"},
    {"key": "nutriactivania", "name": "Nutriactivania", "url": "https://nutriactivania.com/"},
    {"key": "gemygog", "name": "Gemygog", "url": "https://gemygog.com/"},
    {"key": "webconverts", "name": "Webconverts", "url": "https://webconverts.com/"},
)
DAY_ONE_QUESTIONS = (
    {"key": "website_working", "label": "Is the website working?", "path": "/"},
    {"key": "home_page", "label": "Is the home page working?", "path": "/"},
    {"key": "about_page", "label": "Is the About page working?", "path": "/about-us/"},
    {"key": "phone_number", "label": "Is the phone number present and working?", "path": "/contact-us/"},
    {"key": "email", "label": "Is the email address present and working?", "path": "/contact-us/"},
    {"key": "google_map", "label": "Is the Google Map available and working?", "path": "/contact-us/#map"},
    {"key": "form", "label": "Is a contact form available and working?", "path": "/contact-us/#contact-form"},
    {"key": "thank_you_page", "label": "Is the Thank You page available after form submission?", "path": "/thank-you/"},
    {"key": "social_media", "label": "Is social media integration available and working?", "path": "/#social-media"},
)
DAILY_TASK_KEYWORDS = (
    "dental clinic in vasanth nagar",
    "Gum treatment in vasath nagar",
    "wisdom tooth extraction in vasanth nagar",
    "teeth whitening treatment in vasanth nagar",
    "dental care in vasanth nagar",
    "dentist in vasanth nagar",
    "best dentist in vasanth nagar",
    "dental implants in vasanth nagar",
    "cosmetic dentistry in vasanth nagar",
    "dental braces in vasanth nagar",
    "teeth braces in vasanth nagar",
    "invisible braces in vasanth nagar",
    "braces for teeth in vasanth nagar",
    "invisalign aligners in vasanth nagar",
)
DAILY_TASKS = ("rankings", "competitor", "posts")
DAILY_TASK_FIELD_NAMES = (
    "profile_type",
    "business_name",
    "address",
    "contact_number",
    "business_category",
    "website_url",
    "reviews",
    "rating",
    "photo_count",
)
DAILY_POST_FIELD_NAMES = ("image_description", "content", "cta")


def _daily_task_keys_for_day(order_index: int) -> tuple[str, ...]:
    return ("rankings",) if order_index % 2 == 0 else ("posts",)


def _daily_task_prompt(order_index: int) -> str:
    if order_index % 2 == 0:
        return "Complete the keyword rankings. Competitor analysis is locked."
    return "Create three social posts and generate a review."


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
    "Cloud Computing",
    "Web Development",
    "Python Programming",
    "Data Science",
)


def normalize_course_subject(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.split()).casefold()
    return next(
        (subject for subject in COURSE_SUBJECTS if subject.casefold() == normalized),
        None,
    )


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

    db.add(models.Enrollment(
        id=str(uuid.uuid4()),
        student_id=student.id,
        course_id=course.id,
    ))
    db.flush()


def _lesson_question_payload(question: models.LessonQuestion) -> dict:
    return {
        "id": question.id,
        "student_name": question.student.name,
        "question": question.question,
        "trainer_reply": question.trainer_reply,
        "created_at": question.created_at.isoformat() if question.created_at else None,
        "replied_at": question.replied_at.isoformat() if question.replied_at else None,
    }


def _trainer_chat_message_payload(
    message: models.TrainerStudentChatMessage,
    student_id: str,
) -> dict:
    is_student_message = message.sender_id == student_id
    return {
        "id": message.id,
        "message": message.message,
        "sender_role": "student" if is_student_message else "trainer",
        "sender_name": message.sender.name if message.sender else (
            "Student" if is_student_message else "Trainer"
        ),
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def _trainer_chat_enrollment(
    db: Session,
    enrollment_id: str,
    trainer_id: str,
) -> models.Enrollment:
    enrollment = db.query(models.Enrollment).options(
        joinedload(models.Enrollment.student),
        joinedload(models.Enrollment.course),
    ).join(models.Course).filter(
        models.Enrollment.id == enrollment_id,
        models.Course.trainer_id == trainer_id,
    ).first()
    if not enrollment:
        raise HTTPException(status_code=404, detail="Student conversation not found.")
    return enrollment


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
    if len(payload.password.encode("utf-8")) > 1024:
        raise HTTPException(status_code=400, detail="Password must be 1024 bytes or fewer.")
    course_subject = normalize_course_subject(payload.course_subject)
    if not course_subject:
        raise HTTPException(
            status_code=422,
            detail="Choose one of the available major courses.",
        )
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
        role="student",
        course_subject=course_subject,
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
    courses = [e.course.title for e in enrollments if e.course]

    return {
        "name": current_user.name,
        "email": current_user.email,
        "specialization": current_user.specialization,
        "phone_number": current_user.phone_number,
        "role": current_user.role,
        "is_access_granted": current_user.is_access_granted,
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
        selectinload(models.Enrollment.assignment_instructions),
    ).filter_by(student_id=current_user.id).all()
    courses_data = []

    now = datetime.datetime.now(timezone.utc)

    for enr in enrollments:
        modules = enr.course.modules
        progress_by_module = {
            progress.module_id: progress for progress in enr.progress_list
        }
        assignment_instructions_by_module = {
            instruction.module_id: instruction.instructions
            for instruction in enr.assignment_instructions
        }
        modules_list = []
        completed_count = 0

        for idx, mod in enumerate(modules):
            prog = progress_by_module.get(mod.id)

            day_one_complete = _day_one_complete(prog) if mod.order_index == 1 else False
            is_daily_task_day = 2 <= mod.order_index <= 12
            daily_task_complete = (
                _daily_task_complete(prog, mod.order_index)
                if is_daily_task_day and prog and prog.daily_task_state
                else False
            )
            is_video_done = (
                day_one_complete
                if mod.order_index == 1
                else daily_task_complete
                if is_daily_task_day and prog and prog.daily_task_state
                else prog.is_video_completed if prog else False
            )
            assignment_done = (
                day_one_complete
                if mod.order_index == 1
                else daily_task_complete
                if is_daily_task_day and prog and prog.daily_task_state
                else prog.assignment_completed if prog else False
            )
            status_val = (
                "completed" if day_one_complete else "not_started"
            ) if mod.order_index == 1 else (
                prog.assignment_status if prog else "not_started"
            )
            feedback = prog.trainer_feedback if prog else ""

            if is_video_done and assignment_done:
                completed_count += 1

            is_unlocked, lock_reason = _module_unlock_status(
                db, enr, modules, idx, now, progress_by_module
            )

            modules_list.append({
                "module_id": mod.id,
                "order_index": mod.order_index,
                "title": f"Day {mod.order_index}",
                "video_url": (
                    mod.video_url
                    if is_unlocked and mod.order_index != 1
                    else None
                ),
                "assignment_prompt": (
                    mod.assignment_prompt or "Complete the website checks for each site."
                    if mod.order_index == 1
                    else (
                        assignment_instructions_by_module.get(mod.id)
                        or mod.assignment_prompt
                        or _daily_task_prompt(mod.order_index)
                        if is_daily_task_day
                        else mod.assignment_prompt or ""
                    )
                ),
                "has_custom_assignment_instructions": (
                    mod.order_index != 1
                    and mod.id in assignment_instructions_by_module
                ),
                "is_unlocked": is_unlocked,
                "lock_reason": lock_reason,
                "is_video_completed": is_video_done,
                "assignment_completed": assignment_done,
                "assignment_status": status_val,
                "submission_notes": prog.submission_notes if prog else "",
                "trainer_feedback": feedback,
                "day_one": _day_one_payload(prog) if mod.order_index == 1 else None,
                "daily_task": (
                    _daily_task_payload(prog, mod.order_index)
                    if is_daily_task_day
                    else None
                ),
                "review_available": bool(
                    (mod.order_index == 1 and day_one_complete)
                    or (
                        is_daily_task_day
                        and mod.order_index % 2 == 1
                        and prog
                        and _daily_task_complete(prog, mod.order_index)
                    )
                    or (
                        not is_daily_task_day
                        and mod.order_index != 1
                        and mod.order_index % 2 == 1
                        and prog
                        and (
                            prog.assignment_status == "approved"
                            or (prog.assignment_completed and prog.assignment_completed_at)
                        )
                    )
                ),
                "questions": [
                    _lesson_question_payload(question)
                    for question in mod.lesson_questions
                ],
            })

        courses_data.append({
            "enrollment_id": enr.id,
            "course_title": enr.course.title,
            "trainer_name": enr.course.trainer.name if enr.course.trainer else "Trainer",
            "completed_slots": completed_count,
            "modules": modules_list,
        })

    return {
        "courses": courses_data,
    }


@app.get("/student/course-catalog")
def student_course_catalog(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("student")),
):
    if not current_user.is_access_granted:
        raise HTTPException(status_code=403, detail="Your account is awaiting administrative clearance.")

    courses = db.query(models.Course).options(
        joinedload(models.Course.trainer),
    ).order_by(models.Course.title, models.Course.id).all()
    course_ids = [course.id for course in courses]
    request_by_course = {
        request.course_id: request
        for request in db.query(models.CourseAccessRequest).filter(
            models.CourseAccessRequest.student_id == current_user.id,
            models.CourseAccessRequest.course_id.in_(course_ids),
        ).all()
    } if course_ids else {}
    enrolled_course_ids = {
        enrollment.course_id
        for enrollment in db.query(models.Enrollment).filter_by(
            student_id=current_user.id,
        ).all()
    }

    return {
        "courses": [
            {
                "id": course.id,
                "title": course.title,
                "description": course.description,
                "trainer_name": course.trainer.name if course.trainer else "Trainer",
                "status": (
                    "enrolled" if course.id in enrolled_course_ids
                    else request_by_course[course.id].status
                    if course.id in request_by_course
                    else "available"
                ),
            }
            for course in courses
        ],
    }


@app.post("/student/course-access-requests", status_code=201)
def request_course_access(
    payload: schemas.CourseAccessRequestCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("student")),
):
    if not current_user.is_access_granted:
        raise HTTPException(status_code=403, detail="Your account is awaiting administrative clearance.")

    course = db.query(models.Course).filter_by(id=payload.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")
    if db.query(models.Enrollment).filter_by(
        student_id=current_user.id,
        course_id=course.id,
    ).first():
        raise HTTPException(status_code=409, detail="You are already enrolled in this course.")

    access_request = db.query(models.CourseAccessRequest).filter_by(
        student_id=current_user.id,
        course_id=course.id,
    ).first()
    if access_request and access_request.status in {"pending", "approved"}:
        raise HTTPException(status_code=409, detail="A request for this course already exists.")

    if access_request:
        access_request.status = "pending"
        access_request.created_at = datetime.datetime.utcnow()
        access_request.responded_at = None
    else:
        access_request = models.CourseAccessRequest(
            id=str(uuid.uuid4()),
            student_id=current_user.id,
            course_id=course.id,
        )
        db.add(access_request)
    db.commit()
    return {"request_id": access_request.id, "status": "pending"}


@app.get("/trainer/course-access-requests")
def trainer_course_access_requests(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    requests = db.query(models.CourseAccessRequest).join(
        models.Course,
    ).options(
        joinedload(models.CourseAccessRequest.student),
        joinedload(models.CourseAccessRequest.course),
    ).filter(
        models.Course.trainer_id == current_user.id,
        models.CourseAccessRequest.status == "pending",
    ).order_by(models.CourseAccessRequest.created_at).all()
    return {
        "requests": [
            {
                "id": access_request.id,
                "student_id": access_request.student_id,
                "student_name": access_request.student.name,
                "student_email": access_request.student.email,
                "course_id": access_request.course_id,
                "course_title": access_request.course.title,
                "created_at": access_request.created_at.isoformat(),
            }
            for access_request in requests
        ],
    }


@app.post("/trainer/course-access-requests/{request_id}/decision")
def decide_course_access_request(
    request_id: str,
    payload: schemas.CourseAccessDecision,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    access_request = db.query(models.CourseAccessRequest).options(
        joinedload(models.CourseAccessRequest.student),
        joinedload(models.CourseAccessRequest.course),
    ).filter_by(id=request_id).first()
    if not access_request or access_request.course.trainer_id != current_user.id:
        raise HTTPException(status_code=404, detail="Course access request not found.")
    if access_request.status != "pending":
        raise HTTPException(status_code=409, detail="This request has already been decided.")

    if payload.approved:
        _enroll_student_in_course(db, access_request.student, access_request.course)
        access_request.status = "approved"
    else:
        access_request.status = "rejected"
    access_request.responded_at = datetime.datetime.utcnow()
    db.commit()
    return {"request_id": access_request.id, "status": access_request.status}


def _new_day_one_state() -> dict:
    return {
        "visited": [False] * len(DAY_ONE_WEBSITES),
        "answers": [
            [None] * len(DAY_ONE_QUESTIONS)
            for _ in DAY_ONE_WEBSITES
        ],
    }


def _load_day_one_state(progress: models.StudentProgress | None) -> dict:
    if not progress or not progress.day_one_state:
        return _new_day_one_state()

    try:
        state = json.loads(progress.day_one_state)
    except json.JSONDecodeError as error:
        raise HTTPException(
            status_code=500,
            detail="Day 1 progress data is invalid. Please contact support.",
        ) from error

    valid_answers = {"yes", "no", "not_applicable"}
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("visited"), list)
        or len(state["visited"]) != len(DAY_ONE_WEBSITES)
        or any(type(visited) is not bool for visited in state["visited"])
        or not isinstance(state.get("answers"), list)
        or len(state["answers"]) != len(DAY_ONE_WEBSITES)
        or any(
            not isinstance(site_answers, list)
            or len(site_answers) != len(DAY_ONE_QUESTIONS)
            or any(
                answer is not None
                and (not isinstance(answer, str) or answer not in valid_answers)
                for answer in site_answers
            )
            for site_answers in state["answers"]
        )
    ):
        raise HTTPException(
            status_code=500,
            detail="Day 1 progress data is invalid. Please contact support.",
        )
    return state


def _day_one_site_complete(state: dict, website_index: int) -> bool:
    return all(state["answers"][website_index])


def _day_one_complete(progress: models.StudentProgress | None) -> bool:
    if not progress:
        return False
    state = _load_day_one_state(progress)
    return (
        all(state["visited"])
        and all(_day_one_site_complete(state, index) for index in range(len(DAY_ONE_WEBSITES)))
    )


def _day_one_payload(progress: models.StudentProgress | None) -> dict:
    state = _load_day_one_state(progress)
    websites = [
        {
            **website,
            "visited": state["visited"][index],
        }
        for index, website in enumerate(DAY_ONE_WEBSITES)
    ]
    return {
        "websites": websites,
        "questions": list(DAY_ONE_QUESTIONS),
        "answers": state["answers"],
        "complete": _day_one_complete(progress),
    }


def _new_daily_task_state() -> dict:
    return {
        "rankings": None,
        "competitor": None,
        "posts": None,
    }


def _load_daily_task_state(progress: models.StudentProgress | None) -> dict:
    if not progress or not progress.daily_task_state:
        return _new_daily_task_state()
    try:
        state = json.loads(progress.daily_task_state)
    except json.JSONDecodeError as error:
        raise HTTPException(
            status_code=500,
            detail="Daily task progress data is invalid. Please contact support.",
        ) from error

    rankings = state.get("rankings") if isinstance(state, dict) else None
    competitor = state.get("competitor") if isinstance(state, dict) else None
    posts = state.get("posts") if isinstance(state, dict) else None
    if (
        not isinstance(state, dict)
        or set(state) != set(DAILY_TASKS)
        or (rankings is not None and (
            not isinstance(rankings, list)
            or len(rankings) != len(DAILY_TASK_KEYWORDS)
            or any(not isinstance(value, str) or not value.strip() for value in rankings)
        ))
        or (competitor is not None and (
            not isinstance(competitor, list)
            or len(competitor) < 1
            or len(competitor) > 10
            or any(
                not isinstance(profile, dict)
                or set(profile) != set(DAILY_TASK_FIELD_NAMES)
                or profile.get("profile_type") not in {"business", "competitor"}
                or any(
                    not isinstance(profile[field], str) or not profile[field].strip()
                    for field in DAILY_TASK_FIELD_NAMES
                )
                for profile in competitor
            )
            or sum(profile["profile_type"] == "business" for profile in competitor) != 1
        ))
        or (posts is not None and (
            not isinstance(posts, list)
            or len(posts) != 3
            or any(
                not isinstance(post, dict)
                or set(post) != set(DAILY_POST_FIELD_NAMES)
                or any(
                    not isinstance(post[field], str) or not post[field].strip()
                    for field in DAILY_POST_FIELD_NAMES
                )
                for post in posts
            )
        ))
    ):
        raise HTTPException(
            status_code=500,
            detail="Daily task progress data is invalid. Please contact support.",
        )
    return state


def _daily_task_complete(
    progress: models.StudentProgress | None,
    order_index: int,
) -> bool:
    if not progress:
        return False
    state = _load_daily_task_state(progress)
    return all(state[task] is not None for task in _daily_task_keys_for_day(order_index))


def _daily_task_submission_summary(state: dict, order_index: int) -> str:
    lines = []
    if "rankings" in _daily_task_keys_for_day(order_index):
        lines.append("Keyword rankings")
        lines.extend(
            f"- {keyword}: {position}"
            for keyword, position in zip(DAILY_TASK_KEYWORDS, state["rankings"])
        )
        if "competitor" in _daily_task_keys_for_day(order_index):
            lines.append("\nCompetitor analysis")
            for profile in state["competitor"]:
                lines.append(f"\n{profile['profile_type'].title()}")
                lines.extend(
                    f"- {key.replace('_', ' ').title()}: {value}"
                    for key, value in profile.items()
                    if key != "profile_type"
                )
    if "posts" in _daily_task_keys_for_day(order_index):
        for index, post in enumerate(state["posts"], start=1):
            lines.extend((
                f"\nSocial post {index}",
                f"- Image description: {post['image_description']}",
                f"- Content: {post['content']}",
                f"- CTA: {post['cta']}",
            ))
    return "\n".join(lines)


def _daily_task_payload(
    progress: models.StudentProgress | None,
    order_index: int,
) -> dict:
    state = _load_daily_task_state(progress)
    available_tasks = _daily_task_keys_for_day(order_index)
    titles = {
        "rankings": "Keyword rankings",
        "competitor": "Competitor analysis",
        "posts": "Social posts",
    }
    return {
        "keywords": list(DAILY_TASK_KEYWORDS),
        "tasks": [
            {
                "key": task,
                "title": titles[task],
                "complete": state[task] is not None,
                "locked": False,
            }
            for task in available_tasks
        ] + (
            [{
                "key": "competitor",
                "title": titles["competitor"],
                "complete": False,
                "locked": True,
            }]
            if order_index % 2 == 0
            else []
        ),
        "rankings": state["rankings"],
        "competitor": state["competitor"],
        "posts": state["posts"],
        "complete": _daily_task_complete(progress, order_index),
    }


def _get_or_create_day_one_progress(
    db: Session,
    enrollment_id: str,
    module_id: str,
) -> models.StudentProgress:
    progress = db.query(models.StudentProgress).filter_by(
        enrollment_id=enrollment_id,
        module_id=module_id,
    ).first()
    if not progress:
        progress = models.StudentProgress(
            id=str(uuid.uuid4()),
            enrollment_id=enrollment_id,
            module_id=module_id,
            assignment_status="not_started",
        )
        db.add(progress)
    return progress


def _get_or_create_daily_task_progress(
    db: Session,
    enrollment_id: str,
    module_id: str,
) -> models.StudentProgress:
    progress = db.query(models.StudentProgress).filter_by(
        enrollment_id=enrollment_id,
        module_id=module_id,
    ).first()
    if not progress:
        progress = models.StudentProgress(
            id=str(uuid.uuid4()),
            enrollment_id=enrollment_id,
            module_id=module_id,
            assignment_status="not_started",
        )
        db.add(progress)
    return progress


def _module_unlock_status(
    db: Session,
    enrollment: models.Enrollment,
    modules: list[models.Module],
    module_index: int,
    now: datetime.datetime,
    progress_by_module: dict[str, models.StudentProgress] | None = None,
) -> tuple[bool, str]:
    if DAYS_UNLOCKED_FOR_TESTING:
        return True, ""
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
        return False, f"Complete Day {previous_module.order_index} first."
    if (
        previous_module.order_index == 1
        and not _day_one_complete(previous_progress)
    ):
        return False, "Complete all Day 1 website checks first."
    if (
        2 <= previous_module.order_index <= 12
        and previous_progress.daily_task_state
        and not _daily_task_complete(previous_progress, previous_module.order_index)
    ):
        return False, f"Complete all Day {previous_module.order_index} tasks first."
    if not previous_progress.assignment_completed:
        return False, f"Complete the task for Day {previous_module.order_index} first."
    if not previous_progress.assignment_completed_at:
        return False, f"Day {previous_module.order_index} completion time is missing."

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
    modules = db.query(models.Module).filter_by(
        course_id=enrollment.course_id
    ).order_by(models.Module.order_index).all()
    module_index = next(
        (index for index, module in enumerate(modules) if module.id == module_id),
        None
    )
    if module_index is None:
        raise HTTPException(status_code=404, detail="Day not found.")

    is_unlocked, _ = _module_unlock_status(
        db, enrollment, modules, module_index, datetime.datetime.now(timezone.utc)
    )
    if not is_unlocked:
        raise HTTPException(status_code=403, detail="This day is still locked.")
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


@app.get("/student/enrollments/{enrollment_id}/trainer-chat")
def student_trainer_chat(
    enrollment_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("student")),
):
    enrollment = db.query(models.Enrollment).options(
        joinedload(models.Enrollment.course).joinedload(models.Course.trainer),
    ).filter_by(
        id=enrollment_id,
        student_id=current_user.id,
    ).first()
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found.")

    messages = db.query(models.TrainerStudentChatMessage).options(
        joinedload(models.TrainerStudentChatMessage.sender),
    ).filter_by(enrollment_id=enrollment.id).order_by(
        models.TrainerStudentChatMessage.created_at,
        models.TrainerStudentChatMessage.id,
    ).all()
    now = datetime.datetime.now(timezone.utc)
    for message in messages:
        if message.sender_id != current_user.id and not message.student_read_at:
            message.student_read_at = now
    db.commit()
    return {
        "trainer_name": enrollment.course.trainer.name if enrollment.course.trainer else "Trainer",
        "messages": [
            _trainer_chat_message_payload(message, current_user.id)
            for message in messages
        ],
    }


@app.post("/student/enrollments/{enrollment_id}/trainer-chat", status_code=201)
def student_send_trainer_chat_message(
    enrollment_id: str,
    payload: schemas.TrainerStudentChatCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("student")),
):
    enrollment = db.query(models.Enrollment).filter_by(
        id=enrollment_id,
        student_id=current_user.id,
    ).first()
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found.")
    message_text = payload.message.strip()
    if not message_text:
        raise HTTPException(status_code=422, detail="Message cannot be empty.")

    message = models.TrainerStudentChatMessage(
        id=str(uuid.uuid4()),
        enrollment_id=enrollment.id,
        sender_id=current_user.id,
        message=message_text,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return _trainer_chat_message_payload(message, current_user.id)


@app.get("/trainer/chat/unread-count")
def trainer_chat_unread_count(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    unread_count = db.query(func.count(
        models.TrainerStudentChatMessage.id
    )).join(
        models.Enrollment,
        models.Enrollment.id == models.TrainerStudentChatMessage.enrollment_id,
    ).join(
        models.Course,
        models.Course.id == models.Enrollment.course_id,
    ).filter(
        models.Course.trainer_id == current_user.id,
        models.TrainerStudentChatMessage.sender_id == models.Enrollment.student_id,
        models.TrainerStudentChatMessage.trainer_read_at.is_(None),
    ).scalar()
    return {"unread_count": unread_count or 0}


@app.get("/trainer/chat/conversations")
def trainer_chat_conversations(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    enrollments = db.query(models.Enrollment).options(
        joinedload(models.Enrollment.student),
        joinedload(models.Enrollment.course),
    ).join(
        models.Course,
        models.Course.id == models.Enrollment.course_id,
    ).join(
        models.TrainerStudentChatMessage,
        models.TrainerStudentChatMessage.enrollment_id == models.Enrollment.id,
    ).filter(
        models.Course.trainer_id == current_user.id,
    ).distinct().all()
    if not enrollments:
        return {"conversations": []}

    enrollment_ids = [enrollment.id for enrollment in enrollments]
    messages = db.query(models.TrainerStudentChatMessage).filter(
        models.TrainerStudentChatMessage.enrollment_id.in_(enrollment_ids),
    ).order_by(
        models.TrainerStudentChatMessage.created_at,
        models.TrainerStudentChatMessage.id,
    ).all()
    messages_by_enrollment: dict[str, list[models.TrainerStudentChatMessage]] = {}
    for message in messages:
        messages_by_enrollment.setdefault(message.enrollment_id, []).append(message)

    conversations = []
    for enrollment in enrollments:
        conversation_messages = messages_by_enrollment[enrollment.id]
        latest = conversation_messages[-1]
        unread_count = sum(
            message.sender_id == enrollment.student_id and not message.trainer_read_at
            for message in conversation_messages
        )
        conversations.append({
            "enrollment_id": enrollment.id,
            "student_id": enrollment.student_id,
            "student_name": enrollment.student.name if enrollment.student else "Student",
            "student_email": enrollment.student.email if enrollment.student else "",
            "course_title": enrollment.course.title,
            "unread_count": unread_count,
            "last_message": latest.message,
            "last_message_at": latest.created_at.isoformat() if latest.created_at else None,
        })
    conversations.sort(
        key=lambda item: item["last_message_at"] or "",
        reverse=True,
    )
    return {"conversations": conversations}


@app.get("/trainer/chat/{enrollment_id}")
def trainer_get_chat(
    enrollment_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    enrollment = _trainer_chat_enrollment(db, enrollment_id, current_user.id)
    messages = db.query(models.TrainerStudentChatMessage).options(
        joinedload(models.TrainerStudentChatMessage.sender),
    ).filter_by(enrollment_id=enrollment.id).order_by(
        models.TrainerStudentChatMessage.created_at,
        models.TrainerStudentChatMessage.id,
    ).all()
    now = datetime.datetime.now(timezone.utc)
    for message in messages:
        if message.sender_id == enrollment.student_id and not message.trainer_read_at:
            message.trainer_read_at = now
    db.commit()
    return {
        "student_name": enrollment.student.name if enrollment.student else "Student",
        "course_title": enrollment.course.title,
        "messages": [
            _trainer_chat_message_payload(message, enrollment.student_id)
            for message in messages
        ],
    }


@app.post("/trainer/chat/{enrollment_id}", status_code=201)
def trainer_send_chat_message(
    enrollment_id: str,
    payload: schemas.TrainerStudentChatCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    enrollment = _trainer_chat_enrollment(db, enrollment_id, current_user.id)
    message_text = payload.message.strip()
    if not message_text:
        raise HTTPException(status_code=422, detail="Message cannot be empty.")

    message = models.TrainerStudentChatMessage(
        id=str(uuid.uuid4()),
        enrollment_id=enrollment.id,
        sender_id=current_user.id,
        message=message_text,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return _trainer_chat_message_payload(message, enrollment.student_id)


def _student_announcement_enrollment(
    db: Session,
    current_user: models.User,
    enrollment_id: str,
) -> models.Enrollment:
    if not current_user.is_access_granted:
        raise HTTPException(status_code=403, detail="Your account is awaiting clearance.")
    enrollment = db.query(models.Enrollment).filter_by(
        id=enrollment_id,
        student_id=current_user.id,
    ).first()
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found.")
    return enrollment


def _unread_announcement_count(
    db: Session,
    course_id: str,
    student_id: str,
) -> int:
    total_count = db.query(func.count(models.Announcement.id)).filter_by(
        course_id=course_id,
    ).scalar() or 0
    read_count = db.query(func.count(models.AnnouncementRead.announcement_id)).join(
        models.Announcement,
        models.Announcement.id == models.AnnouncementRead.announcement_id,
    ).filter(
        models.Announcement.course_id == course_id,
        models.AnnouncementRead.student_id == student_id,
    ).scalar() or 0
    return total_count - read_count


@app.get("/student/enrollments/{enrollment_id}/announcements/unread-count")
def student_announcement_unread_count(
    enrollment_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("student")),
):
    enrollment = _student_announcement_enrollment(db, current_user, enrollment_id)
    return {
        "unread_count": _unread_announcement_count(
            db,
            enrollment.course_id,
            current_user.id,
        ),
    }


@app.get("/student/enrollments/{enrollment_id}/announcements")
def student_announcements(
    enrollment_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("student")),
):
    enrollment = _student_announcement_enrollment(db, current_user, enrollment_id)
    announcements = db.query(models.Announcement).filter_by(
        course_id=enrollment.course_id,
    ).order_by(models.Announcement.created_at.desc()).all()
    read_ids = {
        receipt.announcement_id
        for receipt in db.query(models.AnnouncementRead).join(
            models.Announcement,
            models.Announcement.id == models.AnnouncementRead.announcement_id,
        ).filter(
            models.Announcement.course_id == enrollment.course_id,
            models.AnnouncementRead.student_id == current_user.id,
        ).all()
    }
    return {
        "announcements": [
            {
                "id": announcement.id,
                "title": announcement.title,
                "message": announcement.message,
                "created_at": announcement.created_at.isoformat(),
                "is_read": announcement.id in read_ids,
            }
            for announcement in announcements
        ],
        "unread_count": _unread_announcement_count(
            db,
            enrollment.course_id,
            current_user.id,
        ),
    }


@app.post("/student/enrollments/{enrollment_id}/announcements/read")
def mark_student_announcements_read(
    enrollment_id: str,
    payload: schemas.AnnouncementReadRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("student")),
):
    enrollment = _student_announcement_enrollment(db, current_user, enrollment_id)
    announcement_ids = set(payload.announcement_ids)
    announcements = db.query(models.Announcement).filter(
        models.Announcement.course_id == enrollment.course_id,
        models.Announcement.id.in_(announcement_ids),
    ).all()
    if len(announcements) != len(announcement_ids):
        raise HTTPException(status_code=404, detail="One or more announcements were not found.")

    existing_ids = {
        receipt.announcement_id
        for receipt in db.query(models.AnnouncementRead).filter(
            models.AnnouncementRead.student_id == current_user.id,
            models.AnnouncementRead.announcement_id.in_(announcement_ids),
        ).all()
    }
    db.add_all(
        models.AnnouncementRead(
            announcement_id=announcement_id,
            student_id=current_user.id,
        )
        for announcement_id in announcement_ids - existing_ids
    )
    db.commit()
    return {
        "unread_count": _unread_announcement_count(
            db,
            enrollment.course_id,
            current_user.id,
        ),
    }


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
        raise HTTPException(status_code=404, detail="Day not found in this enrollment.")
    if module.order_index == 1:
        raise HTTPException(
            status_code=409,
            detail="Complete Day 1 using the website checks in the portal.",
        )
    if 2 <= module.order_index <= 12:
        raise HTTPException(
            status_code=409,
            detail="Complete this day's tasks using the portal task form.",
        )

    progress = db.query(models.StudentProgress).filter_by(
        enrollment_id=enrollment.id,
        module_id=module.id,
    ).first()
    if not progress:
        raise HTTPException(
            status_code=409,
            detail="Complete the day video before marking its task complete.",
        )
    if not progress.is_video_completed:
        raise HTTPException(
            status_code=409,
            detail="Complete the day video before marking its task complete.",
        )
    if not progress.assignment_completed:
        progress.assignment_completed = True
        progress.assignment_completed_at = datetime.datetime.now(timezone.utc)
    db.commit()
    return {"message": "Assignment completion recorded."}


@app.post("/student/daily-task")
def save_daily_task(
    payload: schemas.DailyTaskSubmission,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("student")),
):
    _, module = _get_unlocked_student_module(
        db,
        current_user,
        payload.enrollment_id,
        payload.module_id,
    )
    if not 2 <= module.order_index <= 12:
        raise HTTPException(status_code=404, detail="Daily portal tasks are available for Days 2–12.")
    if payload.task not in _daily_task_keys_for_day(module.order_index):
        raise HTTPException(
            status_code=422,
            detail=f"This task is not scheduled for Day {module.order_index}.",
        )

    progress = _get_or_create_daily_task_progress(
        db,
        payload.enrollment_id,
        module.id,
    )
    if progress.assignment_completed and not progress.daily_task_state:
        raise HTTPException(
            status_code=409,
            detail="This day was already completed before portal tasks were enabled.",
        )

    state = _load_daily_task_state(progress)
    if _daily_task_complete(progress, module.order_index):
        raise HTTPException(status_code=409, detail="All tasks for this day are already complete.")

    if payload.task == "rankings":
        if payload.rankings is None:
            raise HTTPException(status_code=422, detail="Enter a ranking for every keyword.")
        rankings = [value.strip() for value in payload.rankings]
        if any(not value or len(value) > 150 for value in rankings):
            raise HTTPException(
                status_code=422,
                detail="Each keyword needs a ranking or a short 'Not found' response.",
            )
        state["rankings"] = rankings
    elif payload.task == "competitor":
        if payload.competitor_profiles is None:
            raise HTTPException(
                status_code=422,
                detail="Enter the business profile.",
            )
        competitor = [
            {
                key: value.strip()
                for key, value in profile.model_dump().items()
            }
            for profile in payload.competitor_profiles
        ]
        if (
            any(not value for profile in competitor for value in profile.values())
            or sum(profile["profile_type"] == "business" for profile in competitor) != 1
        ):
            raise HTTPException(
                status_code=422,
                detail="Complete the business profile. Use 'Not found' when information is unavailable.",
            )
        state["competitor"] = competitor
    else:
        if payload.posts is None:
            raise HTTPException(status_code=422, detail="Complete all three social posts.")
        posts = [
            {
                key: value.strip()
                for key, value in post.model_dump().items()
            }
            for post in payload.posts
        ]
        if any(not value for post in posts for value in post.values()):
            raise HTTPException(
                status_code=422,
                detail="Each post needs an image description, content, and call to action.",
            )
        state["posts"] = posts

    progress.daily_task_state = json.dumps(state)
    if _daily_task_complete(progress, module.order_index):
        completed_at = datetime.datetime.now(timezone.utc)
        progress.is_video_completed = True
        progress.video_completed_at = completed_at
        progress.assignment_completed = True
        progress.assignment_completed_at = completed_at
        progress.assignment_status = "submitted"
        progress.submitted_at = completed_at
        progress.submission_notes = _daily_task_submission_summary(state, module.order_index)
    db.commit()
    return {"daily_task": _daily_task_payload(progress, module.order_index)}


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
        or (progress.module and progress.module.order_index % 2 == 0)
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
        raise HTTPException(status_code=404, detail="Day progress not found.")
    if progress.module and progress.module.order_index % 2 == 0:
        raise HTTPException(
            status_code=409,
            detail="Review forms are available on odd-numbered days.",
        )
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
        "lesson_title": f"Day {progress.module.order_index}",
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


@app.post("/student/day-one/visit")
def mark_day_one_site_visited(
    payload: schemas.DayOneVisit,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("student")),
):
    _, module = _get_unlocked_student_module(
        db,
        current_user,
        payload.enrollment_id,
        payload.module_id,
    )
    if module.order_index != 1:
        raise HTTPException(status_code=404, detail="Day 1 website checks not found.")

    progress = _get_or_create_day_one_progress(
        db,
        payload.enrollment_id,
        module.id,
    )
    state = _load_day_one_state(progress)
    if any(
        not _day_one_site_complete(state, previous_index)
        for previous_index in range(payload.website_index)
    ):
        raise HTTPException(
            status_code=409,
            detail="Complete the previous website checks before opening this site.",
        )

    state["visited"][payload.website_index] = True
    progress.day_one_state = json.dumps(state)
    db.commit()
    return {"day_one": _day_one_payload(progress)}


@app.post("/student/day-one/answer")
def save_day_one_answer(
    payload: schemas.DayOneAnswer,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("student")),
):
    _, module = _get_unlocked_student_module(
        db,
        current_user,
        payload.enrollment_id,
        payload.module_id,
    )
    if module.order_index != 1:
        raise HTTPException(status_code=404, detail="Day 1 website checks not found.")

    progress = _get_or_create_day_one_progress(
        db,
        payload.enrollment_id,
        module.id,
    )
    state = _load_day_one_state(progress)
    if not state["visited"][payload.website_index]:
        raise HTTPException(
            status_code=409,
            detail="Open the website before answering its checks.",
        )
    if any(
        not _day_one_site_complete(state, previous_index)
        for previous_index in range(payload.website_index)
    ):
        raise HTTPException(
            status_code=409,
            detail="Complete the previous website checks before answering this site.",
        )

    answers = state["answers"][payload.website_index]
    next_question_index = next(
        (index for index, answer in enumerate(answers) if answer is None),
        len(DAY_ONE_QUESTIONS),
    )
    if (
        answers[payload.question_index] is None
        and payload.question_index != next_question_index
    ):
        raise HTTPException(
            status_code=409,
            detail="Answer the previous question before continuing.",
        )

    answers[payload.question_index] = payload.answer
    progress.day_one_state = json.dumps(state)
    if _day_one_complete(progress) and not progress.assignment_completed:
        completed_at = datetime.datetime.now(timezone.utc)
        progress.is_video_completed = True
        progress.video_completed_at = completed_at
        progress.assignment_completed = True
        progress.assignment_completed_at = completed_at
    db.commit()
    return {"day_one": _day_one_payload(progress)}


@app.post("/student/submit-assignment")
def submit_assignment(
    enrollment_id: str = Form(...),
    module_id: str = Form(...),
    submission_text: str = Form(..., min_length=1, max_length=4000),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    _, module = _get_unlocked_student_module(
        db, current_user, enrollment_id, module_id
    )
    if module.order_index == 1:
        raise HTTPException(
            status_code=409,
            detail="Complete Day 1 using the website checks in the portal.",
        )
    if 2 <= module.order_index <= 12:
        raise HTTPException(
            status_code=409,
            detail="Complete this day's tasks using the portal task form.",
        )
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
        raise HTTPException(status_code=422, detail="Provide your response in the portal.")
    if re.search(r"(?:https?://|www\.)", submission_text, re.IGNORECASE):
        raise HTTPException(
            status_code=422,
            detail="External work links are not allowed. Submit your response in the portal.",
        )

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
        selectinload(models.Course.enrollments).selectinload(
            models.Enrollment.assignment_instructions
        ),
    ).filter_by(trainer_id=current_user.id).all()
    courses_payload = []

    for c in courses:
        enrollments = c.enrollments
        modules = c.modules
        student_records = []
        for e in enrollments:
            progress_by_module = {
                progress.module_id: progress for progress in e.progress_list
            }
            assignment_instructions_by_module = {
                instruction.module_id: instruction.instructions
                for instruction in e.assignment_instructions
            }
            completed_count = sum(
                1 for module in modules
                if (
                    progress_by_module.get(module.id)
                    and (
                        _day_one_complete(progress_by_module[module.id])
                        if module.order_index == 1
                        else progress_by_module[module.id].is_video_completed
                        and progress_by_module[module.id].assignment_completed
                    )
                )
            )
            module_progress = []
            for module in modules:
                progress = progress_by_module.get(module.id)
                module_progress.append({
                    "module_id": module.id,
                    "title": f"Day {module.order_index}",
                    "assignment_prompt": (
                        module.assignment_prompt
                        if module.order_index == 1
                        else assignment_instructions_by_module.get(module.id)
                        or module.assignment_prompt
                    ),
                    "assignment_status": (
                        "completed" if _day_one_complete(progress) else "not_started"
                    ) if module.order_index == 1 else (
                        progress.assignment_status if progress else "not_started"
                    ),
                    "is_video_completed": (
                        _day_one_complete(progress)
                        if module.order_index == 1
                        else progress.is_video_completed if progress else False
                    ),
                    "assignment_completed": (
                        _day_one_complete(progress)
                        if module.order_index == 1
                        else progress.assignment_completed if progress else False
                    ),
                })
            student_records.append({
                "enrollment_id": e.id,
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
                    "title": f"Day {module.order_index}",
                    "video_url": None if module.order_index == 1 else module.video_url,
                    "assignment_prompt": module.assignment_prompt,
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
    current_user: models.User = Depends(auth.get_current_user),
):
    if current_user.role != "trainer":
        raise HTTPException(status_code=403, detail="Only trainer accounts can create courses.")

    order_indexes = [module.order_index for module in payload.modules]
    if len(order_indexes) != len(set(order_indexes)):
        raise HTTPException(status_code=422, detail="Day numbers must be unique.")
    if any(order_index > 12 for order_index in order_indexes):
        raise HTTPException(status_code=422, detail="A course can contain at most 12 days.")

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
    modules_by_order = {module.order_index: module for module in payload.modules}
    for order_index in range(1, 13):
        module_payload = modules_by_order.get(order_index)
        module_title = (
            module_payload.title.strip()
            if module_payload
            else f"Day {order_index}"
        )
        video_url = module_payload.video_url.strip() if module_payload else ""
        if not module_title:
            raise HTTPException(
                status_code=422,
                detail="Day title cannot be empty.",
            )
        assignment_prompt = (
            module_payload.assignment_prompt.strip()
            if module_payload and module_payload.assignment_prompt
            else (
                "Complete the website checks for each site."
                if order_index == 1
                else _daily_task_prompt(order_index)
            )
        )
        db.add(models.Module(
            id=str(uuid.uuid4()),
            course_id=course.id,
            order_index=order_index,
            title=module_title,
            video_url=video_url,
            assignment_prompt=assignment_prompt,
        ))
    # A new course must NOT auto-enroll existing students.
    # Trainers assign it explicitly via /trainer/assign-course.
    db.commit()
    return {
        "course_id": course.id,
        "title": course.title,
        "module_count": 12,
    }


@app.put(
    "/trainer/courses/{course_id}/modules/{module_id}/assignment-instructions"
)
def update_trainer_assignment_instructions(
    course_id: str,
    module_id: str,
    payload: schemas.TrainerAssignmentInstructionUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    course = db.query(models.Course).filter_by(
        id=course_id,
        trainer_id=current_user.id,
    ).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")
    module = db.query(models.Module).filter_by(
        id=module_id,
        course_id=course.id,
    ).first()
    if not module:
        raise HTTPException(status_code=404, detail="Course day not found.")

    instructions = payload.instructions.strip()
    if module.order_index == 1:
        if payload.enrollment_id:
            raise HTTPException(
                status_code=422,
                detail="Day 1 instructions are shared by all enrolled students.",
            )
        if not instructions:
            raise HTTPException(
                status_code=422,
                detail="Shared Day 1 instructions cannot be empty.",
            )
        module.assignment_prompt = instructions
    else:
        if not payload.enrollment_id:
            raise HTTPException(
                status_code=422,
                detail="Choose a student to edit this day's assignment.",
            )
        enrollment = db.query(models.Enrollment).filter_by(
            id=payload.enrollment_id,
            course_id=course.id,
        ).first()
        if not enrollment:
            raise HTTPException(status_code=404, detail="Student enrollment not found.")

        assignment_instruction = db.query(
            models.StudentAssignmentInstruction
        ).filter_by(
            enrollment_id=enrollment.id,
            module_id=module.id,
        ).first()
        if instructions:
            if assignment_instruction:
                assignment_instruction.instructions = instructions
            else:
                db.add(models.StudentAssignmentInstruction(
                    id=str(uuid.uuid4()),
                    enrollment_id=enrollment.id,
                    module_id=module.id,
                    instructions=instructions,
                ))
        elif assignment_instruction:
            db.delete(assignment_instruction)

    db.commit()
    return {
        "instructions": (
            instructions
            or module.assignment_prompt
        ),
        "shared": module.order_index == 1,
    }


@app.post("/trainer/courses/{course_id}/announcements", status_code=201)
def create_course_announcement(
    course_id: str,
    payload: schemas.AnnouncementCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("trainer")),
):
    course = db.query(models.Course).filter_by(
        id=course_id,
        trainer_id=current_user.id,
    ).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")
    title = payload.title.strip()
    message = payload.message.strip()
    if not title or not message:
        raise HTTPException(status_code=422, detail="Announcement title and message cannot be empty.")

    announcement = models.Announcement(
        id=str(uuid.uuid4()),
        course_id=course.id,
        trainer_id=current_user.id,
        title=title,
        message=message,
    )
    db.add(announcement)
    db.commit()
    db.refresh(announcement)
    return {
        "id": announcement.id,
        "title": announcement.title,
        "message": announcement.message,
        "created_at": announcement.created_at.isoformat(),
    }


@app.post("/trainer/courses/{course_id}/modules", status_code=201)
def add_trainer_course_module(
    course_id: str,
    payload: schemas.ModuleCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    if current_user.role != "trainer":
        raise HTTPException(status_code=403, detail="Only trainer accounts can add days.")

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
    if next_order_index > 12:
        raise HTTPException(status_code=409, detail="This course already has all 12 days.")
    if payload.order_index != next_order_index:
        raise HTTPException(
            status_code=422,
            detail=f"New day order must be {next_order_index}.",
        )

    title = payload.title.strip()
    video_url = payload.video_url.strip()
    if not title:
        raise HTTPException(
            status_code=422,
            detail="Day title cannot be empty.",
        )

    module = models.Module(
        id=str(uuid.uuid4()),
        course_id=course.id,
        order_index=next_order_index,
        title=title,
        video_url=video_url,
        assignment_prompt=(payload.assignment_prompt or "").strip(),
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

# List students and courses available for direct assignment.
@app.get("/trainer/my-students")
def get_trainer_students(current_user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["trainer", "admin"]:
        raise HTTPException(status_code=403, detail="Access denied")

    if current_user.role == "admin":
        trainer_courses = db.query(models.Course).all()
    else:
        trainer_courses = db.query(models.Course).filter(
            models.Course.trainer_id == current_user.id,
        ).all()

    trainer_course_ids = [c.id for c in trainer_courses]
    enrollments = db.query(models.Enrollment).filter(
        models.Enrollment.course_id.in_(trainer_course_ids)
    ).all() if trainer_course_ids else []
    students_by_id = {}
    for enr in enrollments:
        student = enr.student or db.get(models.User, enr.student_id)
        if not student:
            continue
        students_by_id[student.id] = student

    if current_user.role == "trainer":
        trainer_subject = normalize_course_subject(current_user.specialization)
        matching_students = db.query(models.User).filter_by(
            role="student",
            course_subject=trainer_subject,
            is_access_granted=True,
        ).all() if trainer_subject else []
        for student in matching_students:
            students_by_id[student.id] = student

    result = []
    for student in students_by_id.values():
        student_enrollments = [
            enrollment for enrollment in enrollments
            if enrollment.student_id == student.id
        ]
        enrolled_course_ids = {enrollment.course_id for enrollment in student_enrollments}
        available_to_assign = [
            {"id": c.id, "title": c.title}
            for c in trainer_courses
            if c.id not in enrolled_course_ids
        ]
        result.append({
            "id": student.id,
            "name": student.name,
            "email": student.email,
            "phone_number": student.phone_number,
            "enrolled_courses": [
                {"id": enrollment.course.id, "title": enrollment.course.title}
                for enrollment in student_enrollments
                if enrollment.course
            ],
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
    target_course = db.query(models.Course).filter_by(id=payload.course_id).first()
    if not target_course:
        raise HTTPException(status_code=404, detail="Target course not found")

    if current_user.role == "trainer":
        if target_course.trainer_id != current_user.id:
            raise HTTPException(status_code=403, detail="You can only assign courses that you instruct.")
    elif current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Unauthorized")

    student = db.query(models.User).filter_by(
        id=payload.student_id,
        role="student",
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")
    if current_user.role == "trainer" and (
        normalize_course_subject(student.course_subject)
        != normalize_course_subject(current_user.specialization)
    ):
        raise HTTPException(
            status_code=403,
            detail="You can only directly assign courses to students in your specialization.",
        )

    existing_enr = db.query(models.Enrollment).filter(
        models.Enrollment.student_id == payload.student_id,
        models.Enrollment.course_id == payload.course_id
    ).first()
    if existing_enr:
        raise HTTPException(status_code=400, detail="Student is already enrolled in this course.")

    _enroll_student_in_course(db, student, target_course)
    access_request = db.query(models.CourseAccessRequest).filter_by(
        student_id=student.id,
        course_id=target_course.id,
        status="pending",
    ).first()
    if access_request:
        access_request.status = "approved"
        access_request.responded_at = datetime.datetime.utcnow()

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
    return [
        {
            "progress_id": p.id,
            "student_name": p.enrollment.student.name if p.enrollment.student else "Candidate",
            "student_email": p.enrollment.student.email if p.enrollment.student else "N/A",
            "submission_notes": p.submission_notes or "",
            "submitted_at": p.submitted_at.isoformat() if p.submitted_at else None,
            "course_title": p.enrollment.course.title if p.enrollment.course else "Course",
            "module_title": (
                f"Day {p.module.order_index}" if p.module else "Day"
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
    raise HTTPException(
        status_code=409,
        detail="External work links are not supported. Review the student's portal response instead.",
    )


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
    if prog.module and prog.module.order_index == 1:
        raise HTTPException(
            status_code=409,
            detail="Day 1 is completed through the portal website checks.",
        )
    if prog.assignment_status != "submitted":
        raise HTTPException(status_code=409, detail="This assignment has already been evaluated.")

    prog.is_approved = payload.is_approved
    prog.assignment_status = "approved" if payload.is_approved else "rejected"
    prog.trainer_feedback = payload.feedback
    evaluated_at = datetime.datetime.now(timezone.utc)
    prog.evaluated_at = evaluated_at
    is_portal_task_day = (
        prog.module
        and 2 <= prog.module.order_index <= 12
        and prog.daily_task_state
    )
    if not is_portal_task_day:
        if payload.is_approved:
            prog.assignment_completed = True
            prog.assignment_completed_at = evaluated_at
        else:
            prog.assignment_completed = False
            prog.assignment_completed_at = None
    db.commit()
    if not payload.is_approved:
        message = "Assignment rejected."
    elif is_portal_task_day:
        message = "Evaluation recorded. The day completion time and unlock timer are unchanged."
    else:
        message = "Evaluation recorded. 24-hour unlock cooldown has started."
    return {"message": message}


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
        courses = []
        for enrollment in enrollments:
            progress_by_module = {
                progress.module_id: progress for progress in enrollment.progress_list
            }
            module_progress = []
            for module in enrollment.course.modules if enrollment.course else []:
                progress = progress_by_module.get(module.id)
                if module.order_index == 1:
                    video_completed = _day_one_complete(progress)
                    assignment_completed = video_completed
                else:
                    video_completed = bool(progress and progress.is_video_completed)
                    assignment_completed = bool(progress and progress.assignment_completed)
                module_progress.append({
                    "title": f"Day {module.order_index}",
                    "module_id": module.id,
                    "is_video_completed": video_completed,
                    "assignment_completed": assignment_completed,
                })
            completed_count = sum(
                1 for module in module_progress
                if module["is_video_completed"] and module["assignment_completed"]
            )
            courses.append({
                "enrollment_id": enrollment.id,
                "course_title": enrollment.course.title if enrollment.course else "Course",
                "enrolled_at": enrollment.enrolled_at.isoformat() if enrollment.enrolled_at else None,
                "completed_slots": completed_count,
                "total_slots": len(module_progress),
                "modules": module_progress,
            })
        student_rows.append({
            "student_id": student.id,
            "student_name": student.name,
            "student_email": student.email,
            "phone_number": student.phone_number,
            "course_subject": student.course_subject,
            "is_access_granted": student.is_access_granted,
            "registered_at": student.registered_at.isoformat() if student.registered_at else None,
            "courses": courses,
        })
    return {"students": student_rows}


@app.delete("/admin/students/{student_id}")
def admin_delete_student(
    student_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("admin")),
):
    student = db.query(models.User).filter_by(
        id=student_id,
        role="student",
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")

    db.query(models.DeletionRequest).filter_by(student_id=student.id).delete(
        synchronize_session=False,
    )
    db.delete(student)
    db.commit()
    return {"message": "Student and associated course records were deleted."}


@app.patch("/admin/students/{student_id}/major")
def admin_update_student_major(
    student_id: str,
    payload: schemas.StudentMajorUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_role("admin")),
):
    student = db.query(models.User).filter_by(
        id=student_id,
        role="student",
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")

    course_subject = normalize_course_subject(payload.course_subject)
    if not course_subject:
        raise HTTPException(
            status_code=422,
            detail="Choose one of the available major courses.",
        )

    student.course_subject = course_subject
    db.commit()
    return {
        "student_id": student.id,
        "course_subject": student.course_subject,
    }


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
            progress_by_module = {
                progress.module_id: progress
                for progress in db.query(models.StudentProgress).filter_by(
                    enrollment_id=e.id
                ).all()
            }
            done = sum(
                1
                for module in e.course.modules
                if (
                    _day_one_complete(progress_by_module.get(module.id))
                    if module.order_index == 1
                    else bool(
                        progress_by_module.get(module.id)
                        and progress_by_module[module.id].is_approved
                    )
                )
            ) if e.course else 0
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