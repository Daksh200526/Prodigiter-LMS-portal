import json
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import settings

logger = logging.getLogger(__name__)


class WhatsAppError(RuntimeError):
    pass


def _meta_error_detail(
    response_body: bytes,
    status_code: int,
    access_token: str,
    otp_code: str,
) -> str:
    try:
        result = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"Meta WhatsApp rejected the request (HTTP {status_code})."

    error = result.get("error") if isinstance(result, dict) else None
    if not isinstance(error, dict):
        return f"Meta WhatsApp rejected the request (HTTP {status_code})."

    diagnostics = []
    error_code = error.get("code")
    error_subcode = error.get("error_subcode")
    if error_code is not None:
        diagnostics.append(f"code {error_code}")
    if error_subcode is not None:
        diagnostics.append(f"subcode {error_subcode}")
    message = error.get("message")
    if isinstance(message, str) and message.strip():
        safe_message = message.strip().replace(access_token, "[redacted]")
        safe_message = safe_message.replace(otp_code, "[redacted]")
        diagnostics.append(safe_message)
    suffix = f"; {'; '.join(diagnostics)}" if diagnostics else ""
    return f"Meta WhatsApp rejected the request (HTTP {status_code}{suffix})."


def is_whatsapp_delivery_configured() -> bool:
    return bool(
        settings.WHATSAPP_ACCESS_TOKEN
        and settings.WHATSAPP_PHONE_NUMBER_ID
        and settings.WHATSAPP_OTP_TEMPLATE_NAME
        and settings.WHATSAPP_OTP_TEMPLATE_LANGUAGE
    )


def send_whatsapp_otp(phone_number: str, otp_code: str) -> None:
    if not is_whatsapp_delivery_configured():
        raise WhatsAppError(
            "WhatsApp Cloud API access token, phone number ID, template name, "
            "and template language must be configured."
        )
    if not re.fullmatch(r"[6-9]\d{9}", phone_number):
        raise WhatsAppError(
            "WhatsApp destination must be a valid 10-digit Indian phone number."
        )
    if not re.fullmatch(r"\d{6}", otp_code):
        raise WhatsAppError("OTP must contain exactly six digits.")
    if not re.fullmatch(r"[a-z0-9_]{1,512}", settings.WHATSAPP_OTP_TEMPLATE_NAME):
        raise WhatsAppError(
            "WhatsApp OTP template name must use lowercase letters, digits, "
            "and underscores."
        )

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": f"91{phone_number}",
        "type": "template",
        "template": {
            "name": settings.WHATSAPP_OTP_TEMPLATE_NAME,
            "language": {"code": settings.WHATSAPP_OTP_TEMPLATE_LANGUAGE},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": otp_code}],
                },
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [{"type": "text", "text": otp_code}],
                },
            ],
        },
    }
    request = Request(
        "https://graph.facebook.com/"
        f"{settings.WHATSAPP_GRAPH_API_VERSION}/"
        f"{settings.WHATSAPP_PHONE_NUMBER_ID}/messages",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=10) as response:
            response_body = response.read(4096)
            if response.status not in {200, 201}:
                error_detail = _meta_error_detail(
                    response_body,
                    response.status,
                    settings.WHATSAPP_ACCESS_TOKEN,
                    otp_code,
                )
                logger.warning(
                    "Meta WhatsApp rejected an OTP request: %s.",
                    error_detail,
                )
                raise WhatsAppError(error_detail)
            try:
                result = json.loads(response_body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning(
                    "Meta WhatsApp returned a non-JSON response; HTTP %s.",
                    response.status,
                )
                raise WhatsAppError(
                    "Meta WhatsApp returned an unreadable response. Check the server log."
                ) from None
            messages = result.get("messages") if isinstance(result, dict) else None
            if not isinstance(messages, list) or not messages or not isinstance(
                messages[0], dict
            ) or not messages[0].get("id"):
                logger.warning(
                    "Meta WhatsApp returned an unexpected response; HTTP %s.",
                    response.status,
                )
                raise WhatsAppError(
                    "Meta WhatsApp did not confirm the OTP request. "
                    "Check the server log."
                )
    except HTTPError as error:
        error_detail = _meta_error_detail(
            error.read(4096),
            error.code,
            settings.WHATSAPP_ACCESS_TOKEN,
            otp_code,
        )
        logger.warning(
            "Meta WhatsApp rejected an OTP request: %s.",
            error_detail,
        )
        raise WhatsAppError(error_detail) from None
    except (TimeoutError, URLError, OSError):
        logger.warning("Could not connect to Meta WhatsApp to deliver an OTP.")
        raise WhatsAppError(
            "Could not connect to Meta WhatsApp to deliver the OTP."
        ) from None
