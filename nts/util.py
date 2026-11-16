import collections
import hashlib
import hmac
import os
import re
import secrets
import sys
import time

LOG = collections.deque(maxlen=1000)


def log(msg, level="info", station=None):
    ts = time.time()
    LOG.append({"ts": ts, "level": level, "station": station, "msg": msg})
    prefix = f"[{station}] " if station else ""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {level.upper():5s} {prefix}{msg}", file=sys.stderr, flush=True)


def now():
    return int(time.time())


# ---------- parole ----------
def hash_password(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2${salt}${h}"


def check_password(pw, stored):
    try:
        _, salt, h = stored.split("$")
    except Exception:
        return False
    h2 = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(h, h2)


def gen_password(n=12):
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def slugify(s):
    s = s.lower().strip()
    tr = str.maketrans("ăâîșşțţ", "aaisstt")
    s = s.translate(tr)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "radio"


def safe_filename(name):
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r"[^\w\-. ()\[\]&,'!+]", "_", name, flags=re.UNICODE).strip(" .")
    return name[:200] or "file"


def decode_text(b):
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "cp1250", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


# ---------- clasificare player / dispozitiv ----------
def classify_ua(ua):
    u = (ua or "").lower()
    if "android" in u:
        os_ = "Android"
    elif "iphone" in u or "ipad" in u or "ios" in u or "applecoremedia" in u:
        os_ = "iOS"
    elif "windows" in u or "winamp" in u or "foobar" in u:
        os_ = "Windows"
    elif "mac os" in u or "macintosh" in u or "itunes" in u:
        os_ = "macOS"
    elif "smart-tv" in u or "smarttv" in u or "tizen" in u or "webos" in u:
        os_ = "Smart TV"
    elif "linux" in u:
        os_ = "Linux"
    else:
        os_ = "Altele"
    if "netstreamingradio" in u:
        player = "Aplicația NetStreaming"
    elif "vlc" in u or "libvlc" in u:
        player = "VLC"
    elif "winamp" in u:
        player = "Winamp"
    elif "foobar" in u:
        player = "foobar2000"
    elif "tunein" in u:
        player = "TuneIn"
    elif "radio garden" in u or "radiogarden" in u:
        player = "Radio Garden"
    elif "lavf" in u or "ffmpeg" in u:
        player = "FFmpeg"
    elif "alexa" in u or "echo" in u:
        player = "Alexa"
    elif "edg/" in u:
        player = "Browser (Edge)"
    elif "firefox" in u:
        player = "Browser (Firefox)"
    elif "chrome" in u or "crios" in u:
        player = "Browser (Chrome)"
    elif "safari" in u:
        player = "Browser (Safari)"
    elif "mozilla" in u:
        player = "Browser"
    elif "exoplayer" in u or "stagefright" in u or "dalvik" in u:
        player = "Aplicație Android"
    elif "applecoremedia" in u:
        player = "Aplicație iOS"
    elif not u:
        player = "Necunoscut"
    else:
        player = "Altă aplicație"
    if os_ in ("Android", "iOS"):
        dev = "Mobil"
    elif os_ == "Smart TV" or player == "Alexa":
        dev = "TV / Smart speaker"
    elif os_ in ("Windows", "macOS", "Linux"):
        dev = "PC"
    else:
        dev = "Altele"
    return dev, os_, player
