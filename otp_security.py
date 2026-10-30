from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets

from sqlalchemy.orm import Session

import models
from config import settings

OTP_LENGTH = 6
OTP_TTL_SECONDS = 300
MAX_VERIFY_ATTEMPTS = 3
RESEND_COOLDOWN_SECONDS = 30
MAX_SENDS_PER_HOUR = 5

OTP_PEPPER = settings.OTP_PEPPER.encode()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _phone_hash(phone: str) -> str:
    return hmac.new(OTP_PEPPER, phone.encode(), hashlib.sha256).hexdigest()


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("+91"):
        digits = digits[3:]
    elif digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    digits = digits.lstrip("0")
    if re.fullmatch(r"[6-9]\d{9}", digits):
        return digits
    return None


def generate_secure_otp() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))


def hash_otp(phone: str, otp: str) -> str:
    return hmac.new(OTP_PEPPER, f"{phone}:{otp}".encode(), hashlib.sha256).hexdigest()


def check_rate_limit(db: Session, phone: str) -> tuple[bool, str | None]:
    db.query(models.OTPChallenge).filter(
        models.OTPChallenge.sent_at < _utcnow() - timedelta(hours=2)
    ).delete(synchronize_session=False)
    challenge = db.get(models.OTPChallenge, _phone_hash(phone))
    if not challenge:
        return True, None

    now = _utcnow()
    cooldown_end = challenge.sent_at + timedelta(seconds=RESEND_COOLDOWN_SECONDS)
    if now < cooldown_end:
        wait = int((cooldown_end - now).total_seconds()) + 1
        return False, f"Please wait {wait}s before requesting another OTP."

    if now - challenge.send_window_started_at >= timedelta(hours=1):
        challenge.sends_in_hour = 0
        challenge.send_window_started_at = now
    if challenge.sends_in_hour >= MAX_SENDS_PER_HOUR:
        return False, "Too many OTP requests. Try again in an hour."
    return True, None


def save_otp(db: Session, phone: str, otp: str) -> None:
    now = _utcnow()
    phone_hash = _phone_hash(phone)
    challenge = db.query(models.OTPChallenge).filter_by(phone_hash=phone_hash).with_for_update().first()
    if challenge:
        if now - challenge.send_window_started_at >= timedelta(hours=1):
            challenge.sends_in_hour = 0
            challenge.send_window_started_at = now
        challenge.otp_hash = hash_otp(phone, otp)
        challenge.expires_at = now + timedelta(seconds=OTP_TTL_SECONDS)
        challenge.attempts = 0
        challenge.sent_at = now
        challenge.sends_in_hour += 1
    else:
        db.add(models.OTPChallenge(
            phone_hash=phone_hash,
            otp_hash=hash_otp(phone, otp),
            expires_at=now + timedelta(seconds=OTP_TTL_SECONDS),
            attempts=0,
            sent_at=now,
            sends_in_hour=1,
            send_window_started_at=now,
        ))
    db.commit()


def discard_otp(db: Session, phone: str) -> None:
    challenge = db.get(models.OTPChallenge, _phone_hash(phone))
    if challenge:
        challenge.otp_hash = hash_otp(phone, "")
        challenge.expires_at = _utcnow()
        db.commit()


def verify_and_burn_otp(db: Session, phone: str, raw_otp: str) -> tuple[bool, str | None]:
    challenge = db.query(models.OTPChallenge).filter_by(
        phone_hash=_phone_hash(phone)
    ).with_for_update().first()
    if not challenge:
        return False, "Invalid or expired OTP. Please request a new code."

    if challenge.expires_at < _utcnow():
        db.delete(challenge)
        db.commit()
        return False, "Invalid or expired OTP. Please request a new code."

    expected_hash = challenge.otp_hash
    given_hash = hash_otp(phone, raw_otp)
    if not hmac.compare_digest(expected_hash, given_hash):
        challenge.attempts += 1
        attempts = challenge.attempts
        if attempts >= MAX_VERIFY_ATTEMPTS:
            db.delete(challenge)
        db.commit()
        if attempts >= MAX_VERIFY_ATTEMPTS:
            return False, "Too many invalid attempts. This OTP has been invalidated."
        return False, f"Incorrect OTP. {MAX_VERIFY_ATTEMPTS - attempts} attempt(s) remaining."

    db.delete(challenge)
    db.commit()
    return True, None
