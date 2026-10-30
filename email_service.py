import os
import smtplib
import ssl
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()

# Everything comes from .env. NO secrets or defaults for secrets in code.
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "console")      # console | smtp
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_APP_PASSWORD = os.getenv("SENDER_APP_PASSWORD")
SENDER_NAME = os.getenv("SENDER_NAME", "LMS Portal")


def mask_email(email: str) -> str:
    """asha.k@gmail.com -> a***k@gmail.com (never reveals the whole address)."""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    tail = local[-1] if len(local) > 2 else ""
    return f"{local[:1]}***{tail}@{domain}"


def send_email_otp(recipient_email: str, otp_code: str, valid_minutes: int = 5) -> bool:
    """Email a one-time login code. Returns True on success, False on failure."""
    if not recipient_email:
        print("[EMAIL ERROR] no recipient address")
        return False

    smtp_ready = bool(SENDER_EMAIL and SENDER_APP_PASSWORD)
    if EMAIL_PROVIDER != "smtp" or not smtp_ready:
        # Dev mode: show the OTP in the terminal instead of sending a real email
        print(f"\n[DEV EMAIL] To {recipient_email} | OTP: {otp_code}\n")
        return True

    try:
        # EmailMessage rejects header injection (newlines in addresses) for us
        msg = EmailMessage()
        msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
        msg["To"] = recipient_email
        msg["Subject"] = f"{otp_code} is your {SENDER_NAME} login code"
        msg.set_content(
            f"Hello,\n\n"
            f"Your one-time login code is: {otp_code}\n\n"
            f"It is valid for {valid_minutes} minutes. Never share this code with anyone.\n"
            f"If you did not request it, you can safely ignore this email.\n\n"
            f"- {SENDER_NAME} Team\n"
        )
        msg.add_alternative(
            f"""<div style="font-family:system-ui,Arial,sans-serif;max-width:420px;margin:auto">
  <p>Hello,</p>
  <p>Your one-time login code is:</p>
  <p style="font-size:30px;letter-spacing:6px;font-weight:700;margin:12px 0">{otp_code}</p>
  <p>It is valid for {valid_minutes} minutes. Never share this code with anyone.</p>
  <p style="color:#666;font-size:13px">If you did not request it, you can ignore this email.</p>
</div>""",
            subtype="html",
        )

        context = ssl.create_default_context()
        if SMTP_PORT == 465:  # implicit SSL
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15, context=context) as server:
                server.login(SENDER_EMAIL, SENDER_APP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                if SMTP_USE_TLS:
                    server.starttls(context=context)
                server.login(SENDER_EMAIL, SENDER_APP_PASSWORD)
                server.send_message(msg)
        print(f"[EMAIL SENT] to {mask_email(recipient_email)}")
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {type(e).__name__}: {e}")
        return False