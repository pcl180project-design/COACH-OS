"""
twilio_client.py (Green API Edition)
--------------------------------------
Handles all WhatsApp message sending via Green API.
Drop-in replacement for the Twilio version — all function
signatures are identical so nothing else needs to change.

Green API docs: https://green-api.com/en/docs/
Get your Instance ID + Token at: https://console.green-api.com

Environment variables required (.env):
  GREEN_API_INSTANCE_ID=1234567890
  GREEN_API_TOKEN=your_token_here
  WHATSAPP_GROUP_NUMBER=120363xxxxxxxxx@g.us  ← your group chat ID
"""

import os
import logging
import requests

log = logging.getLogger("CoachOS.whatsapp")


# ── Helpers ────────────────────────────────────────────────────────────────

def _base_url() -> str:
    """Build the Green API base URL from env vars."""
    instance_id = os.getenv("GREEN_API_INSTANCE_ID", "")
    token       = os.getenv("GREEN_API_TOKEN", "")
    if not instance_id or not token:
        raise EnvironmentError(
            "GREEN_API_INSTANCE_ID and GREEN_API_TOKEN must be set in your .env file.\n"
            "Get them from: https://console.green-api.com"
        )
    return f"https://api.green-api.com/waInstance{instance_id}"


def _token() -> str:
    return os.getenv("GREEN_API_TOKEN", "")


def _format_chat_id(number: str) -> str:
    """
    Convert a phone number or group ID into Green API chatId format.

    Individual numbers:  +447700123456        →  447700123456@c.us
    Group IDs:           120363xxxxxxxxx@g.us →  passed through unchanged
    """
    number = number.strip().replace(" ", "")

    # Already a valid chatId (group or individual)
    if number.endswith("@g.us") or number.endswith("@c.us"):
        return number

    # Strip whatsapp: prefix if carried over from old Twilio format
    if number.startswith("whatsapp:"):
        number = number[len("whatsapp:"):]

    # Strip leading +
    if number.startswith("+"):
        number = number[1:]

    return f"{number}@c.us"


# ── Public API (same signatures as the Twilio version) ────────────────────

def send_whatsapp(to: str, body: str, media_url: str = None) -> bool:
    """
    Send a WhatsApp message via Green API.
    Returns True on success, False on failure.

    If media_url is provided, sends as a file/media message.
    """
    if media_url:
        return send_whatsapp_media(to, body, media_url)

    chat_id = _format_chat_id(to)

    try:
        url      = f"{_base_url()}/sendMessage/{_token()}"
        payload  = {
            "chatId":  chat_id,
            "message": body,
        }
        response = requests.post(url, json=payload, timeout=15)
        data     = response.json()

        if response.status_code == 200 and data.get("idMessage"):
            log.debug(f"Message sent → {chat_id} (id: {data['idMessage']})")
            return True
        else:
            log.error(f"Green API error sending to {chat_id}: {data}")
            return False

    except requests.exceptions.Timeout:
        log.error(f"Timeout sending to {chat_id}")
        return False
    except Exception as e:
        log.error(f"Unexpected error sending to {chat_id}: {e}")
        return False


def send_whatsapp_media(to: str, body: str, media_url: str) -> bool:
    """
    Send a WhatsApp message with a media attachment (PDF, video, image, etc.)
    via Green API's sendFileByUrl endpoint.
    """
    chat_id  = _format_chat_id(to)
    filename = media_url.split("/")[-1] or "file"

    try:
        url     = f"{_base_url()}/sendFileByUrl/{_token()}"
        payload = {
            "chatId":   chat_id,
            "urlFile":  media_url,
            "fileName": filename,
            "caption":  body,
        }
        response = requests.post(url, json=payload, timeout=30)
        data     = response.json()

        if response.status_code == 200 and data.get("idMessage"):
            log.debug(f"Media sent → {chat_id} (id: {data['idMessage']})")
            return True
        else:
            log.error(f"Green API media error sending to {chat_id}: {data}")
            return False

    except requests.exceptions.Timeout:
        log.error(f"Timeout sending media to {chat_id}")
        return False
    except Exception as e:
        log.error(f"Unexpected error sending media to {chat_id}: {e}")
        return False


def verify_credentials() -> bool:
    """
    Test that Green API credentials are valid and the instance is connected
    to WhatsApp without sending any message.
    """
    try:
        url      = f"{_base_url()}/getStateInstance/{_token()}"
        response = requests.get(url, timeout=10)
        data     = response.json()
        state    = data.get("stateInstance", "")

        if state == "authorized":
            log.info("Green API instance is authorized and connected ✓")
            return True
        else:
            log.error(
                f"Green API instance state: '{state}' — "
                "open https://console.green-api.com and scan the QR code to connect."
            )
            return False

    except Exception as e:
        log.error(f"Green API credential check failed: {e}")
        return False
