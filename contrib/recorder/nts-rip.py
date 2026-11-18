#!/usr/bin/env python3
"""
nts-rip – înregistrează un stream radio (Icecast / SHOUTcast, MP3 sau AAC) și salvează
fiecare piesă într-un fișier separat, denumit automat după titlul din emisie (ICY metadata).

Utilizare:
  python3 nts-rip.py URL [opțiuni]

Exemple:
  python3 nts-rip.py https://s3.netstreaming.eu/pescobar
  python3 nts-rip.py http://server:8000/live -o /srv/inregistrari --min 90 --offset 2
  python3 nts-rip.py https://s3.netstreaming.eu/pescobar --continuu      # + un fișier cu toată emisia

Opțiuni:
  -o, --out DIR        folderul unde se salvează (implicit ./inregistrari/<nume post>)
  --min SEC            piesele mai scurte de atât sunt șterse (reclame, jingle-uri). Implicit 60
  --max-ore ORE        se oprește după atâtea ore (implicit: nelimitat)
  --offset SEC         mută punctul de tăiere (titlul vine cu întârziere față de sunet: valoare
                       pozitivă = taie mai devreme). Implicit 0
  --pastreaza-prima    păstrează și prima piesă (prinsă de la jumătate), cu sufixul „(incomplet)”
  --continuu           salvează în plus și toată emisia într-un singur fișier
  --ignora TEXT        titluri de ignorat (ex: numele postului, „reclama”). Se poate repeta.

Fără dependențe: doar Python 3.8+ (Linux, Windows, macOS).
"""
import argparse
import datetime as dt
import os
import re
import socket
import ssl
import struct
import sys
import time
import urllib.parse

UA = "nts-rip/1.0"


def log(msg):
    print(f"{dt.datetime.now():%H:%M:%S} {msg}", flush=True)


def safe_name(s, maxlen=150):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s).strip(" .")
    s = re.sub(r"\s+", " ", s)
    return (s[:maxlen].rstrip(" .") or "Necunoscut")


def decode(b):
    for enc in ("utf-8", "cp1250", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", "replace")


# ---------------------------------------------------------------- ID3 (MP3)
def id3_tag(title, artist, station, comment):
    def frame(fid, text):
        data = b"\x01" + text.encode("utf-16")          # UTF-16 cu BOM
        return fid.encode() + struct.pack(">I", len(data)) + b"\x00\x00" + data
    frames = b""
    if title:
        frames += frame("TIT2", title)
    if artist:
        frames += frame("TPE1", artist)
    if station:
        frames += frame("TALB", station)
    frames += frame("TYER", str(dt.date.today().year))
    size = len(frames)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x03\x00\x00" + syncsafe + frames


def mp3_sync(buf, start=0):
    """poziția primului cadru MP3 valid de la start (sau start dacă nu găsește)"""
    n = len(buf)
    i = start
    while i < n - 4 and i < start + 8192:
        if buf[i] == 0xFF and (buf[i + 1] & 0xE0) == 0xE0 and (buf[i + 1] & 0x06) != 0 and \
                (buf[i + 2] & 0xF0) != 0xF0 and (buf[i + 2] & 0x0C) != 0x0C:
            return i
        i += 1
    return start


def adts_sync(buf, start=0):
    n = len(buf)
    i = start
    while i < n - 2 and i < start + 8192:
        if buf[i] == 0xFF and (buf[i + 1] & 0xF6) == 0xF0:
            return i
        i += 1
    return start


# ---------------------------------------------------------------- conexiune
def open_stream(url, timeout=20, redirects=5):
    for _ in range(redirects + 1):
        u = urllib.parse.urlsplit(url)
        tls = u.scheme == "https"
        port = u.port or (443 if tls else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        req = (f"GET {path} HTTP/1.0\r\nHost: {u.hostname}\r\nUser-Agent: {UA}\r\n"
               f"Icy-MetaData: 1\r\nAccept: */*\r\nConnection: close\r\n\r\n")
        sock.sendall(req.encode())
        f = sock.makefile("rb")
        status = f.readline().decode("latin-1").strip()
        headers = {}
        while True:
            line = f.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            if b":" in line:
                k, v = line.decode("latin-1").split(":", 1)
                headers[k.strip().lower()] = v.strip()
        parts = status.split(" ")
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if code in (301, 302, 303, 307, 308) and "location" in headers:
            sock.close()
            url = urllib.parse.urljoin(url, headers["location"])
            continue
        if code != 200:
            sock.close()
            raise ConnectionError(f"serverul a răspuns: {status}")
        ctype = headers.get("content-type", "").lower()
        if "mpegurl" in ctype or "scpls" in ctype or "x-pls" in ctype:
            body = f.read(65536).decode("utf-8", "replace")
            sock.close()
            for line in body.splitlines():
                line = line.strip()
                if line.lower().startswith("file") and "=" in line:
                    line = line.split("=", 1)[1].strip()
                if line.startswith(("http://", "https://")):
                    url = line
                    break
            else:
                raise ConnectionError("playlist fără adresă de stream")
            continue
        return sock, f, headers, url
    raise ConnectionError("prea multe redirecționări")


# ---------------------------------------------------------------- înregistrare
class Ripper:
    def __init__(self, args):
        self.a = args
        self.cur_file = None
        self.cur_path = None
        self.cur_title = None
        self.cur_start = None
        self.cur_bytes = 0
        self.first = True
        self.cont = None
        self.ext = ".mp3"
        self.sync = mp3_sync
        self.station = ""
        self.byterate = 16000
        self.pending = bytearray()      # audio încă nescris (pentru --offset)
        self.split_at = None            # (poziție în pending, titlu nou)
        self.ignored = [x.lower() for x in (args.ignora or [])]
        self.saved = 0

    def setup(self, headers):
        ctype = headers.get("content-type", "audio/mpeg").lower()
        if "aac" in ctype or "mp4" in ctype:
            self.ext, self.sync = ".aac", adts_sync
        elif "ogg" in ctype:
            self.ext, self.sync = ".ogg", (lambda b, s=0: s)
        self.station = headers.get("icy-name") or headers.get("ice-name") or ""
        try:
            self.byterate = int(float(headers.get("icy-br", "128").split(",")[0])) * 125
        except ValueError:
            self.byterate = 16000
        if not self.a.out:
            self.a.out = os.path.join("inregistrari", safe_name(self.station or "radio"))
        os.makedirs(self.a.out, exist_ok=True)
        for fn in os.listdir(self.a.out):            # resturi de la o oprire bruscă
            if fn.startswith(".rec-") and fn.endswith(".part") and fn != os.path.basename(self.cur_path or ""):
                try:
                    os.remove(os.path.join(self.a.out, fn))
                except OSError:
                    pass
        if self.a.continuu and self.cont is None:
            p = os.path.join(self.a.out, f"emisie-{dt.datetime.now():%Y%m%d-%H%M%S}{self.ext}")
            self.cont = open(p, "wb")
            log(f"înregistrare continuă: {p}")

    def tracklist(self, line):
        with open(os.path.join(self.a.out, "tracklist.txt"), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def is_ignored(self, title):
        t = (title or "").strip().lower()
        if not t:
            return True
        if self.station and t == self.station.strip().lower():
            return True
        return any(x in t for x in self.ignored)

    def open_track(self, title):
        self.cur_title = title
        self.cur_start = time.time()
        self.cur_bytes = 0
        if self.is_ignored(title):
            self.cur_file = None
            log(f"⏭  ignor: {title or '(fără titlu)'}")
            return
        tmp = os.path.join(self.a.out, f".rec-{int(time.time() * 1000)}{self.ext}.part")
        self.cur_path = tmp
        self.cur_file = open(tmp, "wb")
        self.partial = self.first
        log(f"⏺  {title}" + ("   (prinsă de la jumătate)" if self.first else ""))

    def close_track(self, complete=True):
        if not self.cur_file:
            return
        self.cur_file.close()
        self.cur_file = None
        dur = self.cur_bytes / max(1, self.byterate)
        title = self.cur_title or "Necunoscut"
        keep = complete and dur >= self.a.min and (not self.partial or self.a.pastreaza_prima)
        if not keep:
            os.remove(self.cur_path)
            why = "incompletă" if (self.partial and not self.a.pastreaza_prima) or not complete else \
                f"prea scurtă ({dur:.0f}s)"
            log(f"🗑  șters: {title} – {why}")
            return
        name = safe_name(title) + (" (incomplet)" if self.partial else "")
        dest = os.path.join(self.a.out, name + self.ext)
        n = 2
        while os.path.exists(dest):
            dest = os.path.join(self.a.out, f"{name} ({n}){self.ext}")
            n += 1
        if self.ext == ".mp3":
            artist, _, song = title.partition(" - ")
            if not song:
                artist, song = "", title
            with open(self.cur_path, "rb") as src, open(dest, "wb") as out:
                out.write(id3_tag(song.strip(), artist.strip(), self.station, self.a.url))
                while True:
                    b = src.read(1 << 20)
                    if not b:
                        break
                    out.write(b)
            os.remove(self.cur_path)
        else:
            os.replace(self.cur_path, dest)
        self.saved += 1
        m, s = divmod(int(dur), 60)
        log(f"✅ salvat: {os.path.basename(dest)}  [{m}:{s:02d}]")
        self.tracklist(f"{dt.datetime.fromtimestamp(self.cur_start):%Y-%m-%d %H:%M:%S}\t{m}:{s:02d}\t{title}")

    def write_audio(self, data):
        if self.cont:
            self.cont.write(data)
        if self.cur_file:
            self.cur_file.write(data)
        self.cur_bytes += len(data)

    def do_split(self, pos, title):
        cut = self.sync(self.pending, max(0, min(pos, len(self.pending))))
        self.write_audio(bytes(self.pending[:cut]))
        del self.pending[:cut]
        self.close_track()
        self.first = False
        self.open_track(title)
        self.split_at = None

    def feed(self, data):
        """audio nou. Cu --offset pozitiv ținem câteva secunde în buffer ca să putem tăia mai devreme;
        cu --offset negativ amânăm tăierea până se adună audio-ul în plus."""
        self.pending += data
        if self.split_at is not None and len(self.pending) >= self.split_at[0]:
            self.do_split(*self.split_at)
        if self.split_at is None:
            lag = int(max(0.0, self.a.offset) * self.byterate)
            if len(self.pending) > lag:
                n = len(self.pending) - lag
                self.write_audio(bytes(self.pending[:n]))
                del self.pending[:n]

    def on_title(self, title):
        if title == self.cur_title and self.split_at is None:
            return
        if self.cur_title is None:
            self.open_track(title)
            return
        if self.split_at is not None:          # titlul s-a schimbat din nou înainte de tăiere
            self.do_split(*self.split_at)
            if title == self.cur_title:
                return
        shift = int(self.a.offset * self.byterate)   # pozitiv = taie mai devreme
        self.split_at = (max(0, len(self.pending) - shift), title)
        self.feed(b"")

    def run(self):
        deadline = time.time() + self.a.max_ore * 3600 if self.a.max_ore else None
        backoff = 3
        while True:
            try:
                sock, f, headers, url = open_stream(self.a.url)
                self.setup(headers)
                metaint = int(headers.get("icy-metaint") or 0)
                log(f"conectat la {self.station or url}  ({headers.get('content-type', '?')}, "
                    f"{self.byterate // 125} kbps) → {os.path.abspath(self.a.out)}")
                if not metaint:
                    log("ATENȚIE: serverul nu trimite titlurile pieselor (ICY metadata) – nu pot separa piesele.")
                backoff = 3
                while True:
                    if deadline and time.time() > deadline:
                        log("timpul maxim a expirat")
                        return
                    if metaint:
                        audio = f.read(metaint)
                        if len(audio) < metaint:
                            raise ConnectionError("stream întrerupt")
                        self.feed(audio)
                        ln = f.read(1)
                        if not ln:
                            raise ConnectionError("stream întrerupt")
                        n = ln[0] * 16
                        if n:
                            meta = f.read(n)
                            m = re.search(rb"StreamTitle='(.*?)';", meta, re.S)
                            if m:
                                self.on_title(decode(m.group(1)).strip())
                    else:
                        audio = f.read(16384)
                        if not audio:
                            raise ConnectionError("stream întrerupt")
                        if self.cur_title is None:
                            self.open_track(self.station or "emisie")
                        self.feed(audio)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"conexiune pierdută: {e} – reîncerc în {backoff}s")
                # piesa întreruptă de o cădere nu mai e completă
                self.close_track(complete=False)
                self.cur_title = None
                self.first = True
                self.pending.clear()
                self.split_at = None
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def finish(self):
        if self.pending:
            self.write_audio(bytes(self.pending))
            self.pending.clear()
        self.close_track(complete=False)
        if self.cont:
            self.cont.close()
        log(f"oprit. Piese salvate: {self.saved}")


def main():
    ap = argparse.ArgumentParser(description="Înregistrează piesele dintr-un stream radio, separate și denumite automat.")
    ap.add_argument("url")
    ap.add_argument("-o", "--out")
    ap.add_argument("--min", type=float, default=60)
    ap.add_argument("--max-ore", type=float, default=0)
    ap.add_argument("--offset", type=float, default=0)
    ap.add_argument("--pastreaza-prima", action="store_true")
    ap.add_argument("--continuu", action="store_true")
    ap.add_argument("--ignora", action="append")
    args = ap.parse_args()
    import signal

    def stop(*_):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, stop)      # systemctl stop / kill -> salvează curat
    except (AttributeError, ValueError):
        pass
    r = Ripper(args)
    try:
        r.run()
    except KeyboardInterrupt:
        print()
    finally:
        r.finish()


if __name__ == "__main__":
    main()
