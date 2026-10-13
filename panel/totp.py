"""Одноразовые коды для двухфакторного входа (TOTP, RFC 6238) — без внешних зависимостей.

Совместимо с Google Authenticator, Яндекс Ключом, Microsoft Authenticator, Aegis и т.п.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import time
import urllib.parse

STEP = 30          # секунд на код
DIGITS = 6


def new_secret():
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _key(secret):
    s = secret.replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret, counter, digits=DIGITS, digest=hashlib.sha1):
    h = hmac.new(_key(secret), struct.pack(">Q", counter), digest).digest()
    o = h[-1] & 0x0F
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


def verify(secret, code, last_counter=None, window=1, now=None):
    """Номер шага, если код верен (±window шагов на расхождение часов), иначе None.
    Шаги не новее last_counter отвергаются — один и тот же код нельзя использовать дважды."""
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{%d}" % DIGITS, code):
        return None
    cur = int((time.time() if now is None else now) // STEP)
    for c in range(cur - window, cur + window + 1):
        if last_counter is not None and c <= last_counter:
            continue
        if hmac.compare_digest(code_at(secret, c), code):
            return c
    return None


def uri(secret, account, issuer):
    label = urllib.parse.quote(f"{issuer}:{account}")
    q = urllib.parse.urlencode({"secret": secret, "issuer": issuer, "digits": DIGITS, "period": STEP})
    return f"otpauth://totp/{label}?{q}"


# ---------------------------------------------------------------- запасные коды

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"     # без похожих символов: 0/o, 1/l/i


def new_recovery_codes(n=8):
    return ["".join(secrets.choice(_ALPHABET) for _ in range(4)) + "-" +
            "".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(n)]


def hash_recovery(code):
    norm = re.sub(r"[^a-z0-9]", "", code.lower())
    return hashlib.sha256(norm.encode()).hexdigest()
