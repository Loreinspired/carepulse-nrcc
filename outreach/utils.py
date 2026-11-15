import io
import re
from urllib.parse import quote

import qrcode
import qrcode.image.svg


class PhoneNormalizationError(ValueError):
    """Raised when a phone number cannot be normalized to valid E.164 format."""


_NON_DIGIT_KEEP_PLUS_RE = re.compile(r"[^\d+]")
_NG_E164_RE = re.compile(r"^\+234[789]\d{9}$")
_INTL_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_nigeria_phone(raw_phone: str) -> str:
    """
    Normalize Nigerian (and explicit international) phone numbers into E.164 format.

    Supported Nigerian formats:
      - 08031234567 / 0803 123 4567 / 0803-123-4567 -> +2348031234567
      - 8031234567 (10 digits starting with 7/8/9)    -> +2348031234567
      - 2348031234567                                -> +2348031234567
      - +2348031234567                               -> +2348031234567
      - +23408031234567 / 23408031234567             -> +2348031234567
      - 002348031234567                              -> +2348031234567
    """
    if raw_phone is None:
        raise PhoneNormalizationError("Phone number is required.")

    cleaned = str(raw_phone).strip()
    if not cleaned:
        raise PhoneNormalizationError("Phone number cannot be empty.")

    # Reject inputs containing alphabetic characters
    if re.search(r"[A-Za-z]", cleaned):
        raise PhoneNormalizationError(f"Invalid characters in phone number: {raw_phone}")

    # Strip spaces, hyphens, parentheses, dots, etc.
    compact = _NON_DIGIT_KEEP_PLUS_RE.sub("", cleaned)
    if not compact:
        raise PhoneNormalizationError(f"No digits found in phone number: {raw_phone}")

    # Normalize international '00' prefix to '+'
    if compact.startswith("00"):
        compact = "+" + compact[2:]

    # Collapse any stray '+' not at index 0
    if "+" in compact[1:]:
        raise PhoneNormalizationError(f"Malformed '+' placement in phone number: {raw_phone}")

    # Case 1: Starts with +234 or 234
    if compact.startswith("+234") or compact.startswith("234"):
        digits = compact.lstrip("+")
        national = digits[3:]
        # Handle common Nigerian input mistake: +2340803...
        if national.startswith("0") and len(national) == 11:
            national = national[1:]
        candidate = f"+234{national}"
        if not _NG_E164_RE.match(candidate):
            raise PhoneNormalizationError(
                f"Invalid Nigerian mobile number '{raw_phone}'. Expected 10 digits after +234 starting with 7, 8, or 9."
            )
        return candidate

    # Case 2: Explicit non-Nigerian international number
    if compact.startswith("+"):
        if not _INTL_E164_RE.match(compact):
            raise PhoneNormalizationError(f"Invalid international E.164 phone number: {raw_phone}")
        return compact

    # Case 3: Local Nigerian 11-digit format starting with 0 (e.g., 08031234567)
    if compact.startswith("0"):
        national = compact[1:]
        candidate = f"+234{national}"
        if not _NG_E164_RE.match(candidate):
            raise PhoneNormalizationError(
                f"Invalid Nigerian local number '{raw_phone}'. Expected 11 digits like 08031234567."
            )
        return candidate

    # Case 4: 10-digit Nigerian number missing leading 0 (e.g., 8031234567)
    if len(compact) == 10 and compact[0] in ("7", "8", "9"):
        candidate = f"+234{compact}"
        if _NG_E164_RE.match(candidate):
            return candidate

    raise PhoneNormalizationError(
        f"Unrecognized phone format '{raw_phone}'. Enter e.g. 08031234567 or +2348031234567."
    )


def e164_to_wa_id(phone_e164: str) -> str:
    """Convert E.164 (+2348031234567) to Meta wa_id digits (2348031234567)."""
    return (phone_e164 or "").lstrip("+").strip()


def build_wa_me_url(phone_e164: str, text: str = "") -> str:
    """Build a 1-tap https://wa.me/<digits>?text=<encoded> deep link."""
    digits = re.sub(r"\D", "", phone_e164 or "")
    if not digits:
        return "https://wa.me/"
    if text:
        return f"https://wa.me/{digits}?text={quote(text)}"
    return f"https://wa.me/{digits}"


def generate_qr_svg(data: str, box_size: int = 8, border: int = 2) -> str:
    """Render an inline SVG QR code string for the given URL/data."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    img.save(buffer)
    svg_text = buffer.getvalue().decode("utf-8")
    # Make SVG responsive inside Tailwind containers
    svg_text = svg_text.replace("<svg ", '<svg class="w-full h-full" ', 1)
    return svg_text
