import json
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import settings

SMS_ALERT_SEND_URL = "https://www.smsalert.co.in/api/push.json"
logger = logging.getLogger(__name__)

HTTP_ERROR_HINTS = {
    400: "Check the sender ID, phone number, and approved message template.",
    401: "The SMS Alert API key was rejected. Check the key in your local .env file.",
    402: "SMS Alert reports a plan or credit limit. Check your account balance and plan.",
    403: "The account does not have permission to use this sender ID or endpoint.",
    404: "SMS Alert did not recognize the API endpoint.",
    429: "SMS Alert is rate-limiting requests. Wait before requesting another OTP.",
}


class SmsAlertError(RuntimeError):
    pass


def is_sms_delivery_configured() -> bool:
    return bool(
        settings.SMS_ALERT_API_KEY
        and settings.SMS_ALERT_SENDER_ID
        and "{otp}" in settings.SMS_ALERT_OTP_MESSAGE
    )


def send_sms_otp(phone_number: str, otp_code: str) -> None:
    if not is_sms_delivery_configured():
        raise SmsAlertError("SMS Alert API key and sender ID must be configured.")
    if not re.fullmatch(r"\d{10}", phone_number):
        raise SmsAlertError("SMS destination must be a valid 10-digit Indian phone number.")
    if not re.fullmatch(r"\d{6}", otp_code):
        raise SmsAlertError("OTP must contain exactly six digits.")
    if "{otp}" not in settings.SMS_ALERT_OTP_MESSAGE:
        raise SmsAlertError("SMS_ALERT_OTP_MESSAGE must contain the {otp} placeholder.")

    message = settings.SMS_ALERT_OTP_MESSAGE.replace("{otp}", otp_code)
    payload = {
        "apikey": settings.SMS_ALERT_API_KEY,
        "sender": settings.SMS_ALERT_SENDER_ID,
        "mobileno": f"+91{phone_number}",
        "text": message,
    }
    encoded_payload = urlencode(payload).encode("utf-8")
    request = Request(
        f"{SMS_ALERT_SEND_URL}?{encoded_payload.decode('utf-8')}",
        data=encoded_payload,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        },
    )

    try:
        with urlopen(request, timeout=10) as response:
            if response.status not in {200, 202}:
                raise SmsAlertError(
                    _http_error_message(response.status)
                )
            response_body = response.read(4096)
            try:
                result = json.loads(response_body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning(
                    "SMS Alert returned a non-JSON response; HTTP %s.",
                    response.status,
                )
                raise SmsAlertError(
                    "SMS Alert returned an unreadable response. Check the server log."
                ) from None
            if not isinstance(result, dict):
                logger.warning(
                    "SMS Alert returned an unexpected response type; HTTP %s.",
                    response.status,
                )
                raise SmsAlertError(
                    "SMS Alert returned an unexpected response. Check the server log."
                )
            provider_status = str(result.get("status", "")).strip().lower()
            if provider_status not in {"success", "accepted"}:
                logger.warning(
                    "SMS Alert rejected an OTP request; HTTP %s, provider status %s.",
                    response.status,
                    provider_status or "missing",
                )
                raise SmsAlertError(
                    "SMS Alert did not accept the OTP request. Check the server log "
                    "for the provider response status."
                )
    except HTTPError as error:
        message = _http_error_message(error.code)
        logger.warning("SMS Alert rejected an OTP request: HTTP %s.", error.code)
        raise SmsAlertError(message) from None
    except (TimeoutError, URLError, OSError):
        logger.warning("Could not connect to SMS Alert to deliver an OTP.")
        raise SmsAlertError("Could not connect to SMS Alert to deliver the OTP.") from None


def _http_error_message(status_code: int) -> str:
    hint = HTTP_ERROR_HINTS.get(
        status_code,
        "Check the SMS Alert service status and try again.",
    )
    return f"SMS Alert rejected the OTP request (HTTP {status_code}). {hint}"
