import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_list(value: str | None, default: List[str]) -> List[str]:
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings:
    APP_ENV = os.getenv("APP_ENV", "development")
    DEBUG = _as_bool(os.getenv("DEBUG"), APP_ENV == "development")
    DEV_MODE = _as_bool(os.getenv("DEV_MODE"), False)
    if DEV_MODE and APP_ENV != "development":
        raise RuntimeError("DEV_MODE can only be enabled when APP_ENV=development.")

    DB_USER = os.getenv("DB_USER", "lms_app")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DB_HOST = os.getenv("DB_HOST", "localhost")
    DB_PORT = os.getenv("DB_PORT", "3306")
    DB_NAME = os.getenv("DB_NAME", "lms_db")

    if os.getenv("DATABASE_URL"):
        DATABASE_URL = os.getenv("DATABASE_URL")
    elif DB_PASSWORD:
        DATABASE_URL = (
            f"mysql+pymysql://{DB_USER}:{os.getenv('DB_PASSWORD','')}@"
            f"{DB_HOST}:{DB_PORT}/{DB_NAME}"
        )
    else:
        DATABASE_URL = f"sqlite:///{(BASE_DIR / 'lms.db').as_posix()}"

    def _require_secret(name: str, value: str | None, min_length: int = 32) -> str:
        if not value:
            raise RuntimeError(f"{name} is missing. Set it in your .env file.")
        value = value.strip()
        if len(value) < min_length:
            raise RuntimeError(f"{name} must be at least {min_length} characters long.")
        placeholder_values = {
            "dev-only-secret-change-me",
            "dev-only-otp-pepper",
            "change-me",
            "replace-me",
            "your-secret-key",
            "your-otp-pepper",
        }
        if value.lower() in placeholder_values:
            raise RuntimeError(f"{name} must not use a placeholder or demo value.")
        return value

    JWT_SECRET_KEY = _require_secret("JWT_SECRET_KEY", os.getenv("JWT_SECRET_KEY"))
    OTP_PEPPER = _require_secret("OTP_PEPPER", os.getenv("OTP_PEPPER"))
    WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "EAAXS6AAfprgBS83vN6l1bd9tyAQaFokjZBRPZBWld6dKjgCn30t4IZBIYKK64nnpZCZAgpadR7CTedWa1ZC3YWZCmWHe11Tgf3cc3XYZBLe15HfYSKLdZC2LjEWk45qOmsfLzLlwg9t0cJRgZBhZB7ImgJPQnivEDoCeBLsxX4PKoQAG0xiW0h8bHxUjy3qhrr2I2ZCQ2QZDZD").strip()
    WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "108588198983800").strip()
    WHATSAPP_OTP_TEMPLATE_NAME = os.getenv(
        "WHATSAPP_OTP_TEMPLATE_NAME",
        "login_otp",
    ).strip()
    WHATSAPP_OTP_TEMPLATE_LANGUAGE = os.getenv(
        "WHATSAPP_OTP_TEMPLATE_LANGUAGE",
        "en_US",
    ).strip()
    WHATSAPP_GRAPH_API_VERSION = os.getenv(
        "WHATSAPP_GRAPH_API_VERSION",
        "v26.0",
    ).strip()
    assignment_webhook_secret = os.getenv("ASSIGNMENT_WEBHOOK_SECRET")
    if assignment_webhook_secret:
        ASSIGNMENT_WEBHOOK_SECRET = _require_secret(
            "ASSIGNMENT_WEBHOOK_SECRET",
            assignment_webhook_secret,
        )
    else:
        ASSIGNMENT_WEBHOOK_SECRET = None
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    REVIEW_REDIRECT_URL = os.getenv(
        "REVIEW_REDIRECT_URL",
        "https://example.com/reviews",
    ).strip()
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

    ALLOWED_ORIGINS = _as_list(
        os.getenv("ALLOWED_ORIGINS"),
        ["http://localhost:8000", "http://127.0.0.1:8000"],
    )


settings = Settings()
