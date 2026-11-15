"""Motorul audio NtS-Radio.

Flux per post:
  sursă live (studio)  ─► ffmpeg decode ─┐
                                          ├─► MIXER (PCM 44.1k stereo, ritm real) ─► ffmpeg encode (mp3/aac) ─► Hub ─► ascultători
  AutoDJ (fișiere)     ─► ffmpeg decode ─┘
Mixerul alege sursa live când e disponibilă, altfel AutoDJ, altfel liniște.
Ascultătorii nu sunt deconectați niciodată la comutare (encoderul rulează continuu).
"""
import asyncio
import collections
import json
import os
import random
import shutil
import time
from array import array

from .util import log, now, decode_text, classify_ua

SR = 44100
CH = 2
BPS = SR * CH * 2                     # bytes / secundă PCM
CHUNK_MS = 50
CHUNK = BPS * CHUNK_MS // 1000        # 8820 bytes
AUDIO_EXT = {".mp3", ".aac", ".m4a", ".ogg", ".opus", ".flac", ".wav", ".wma", ".mp2"}
SILENT_PEAK = 33                      # ~ -60 dBFS

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


def _clip(v):
    return 32767 if v > 32767 else (-32768 if v < -32768 else v)


def mix(a, b, ga, gb):
    x = array("h"); x.frombytes(a)
    y = array("h"); y.frombytes(b)
    return array("h", [_clip(int(p * ga + q * gb)) for p, q in zip(x, y)]).tobytes()


def gain(a, g):
    if g >= 0.999:
        return a
    x = array("h"); x.frombytes(a)
    return array("h", [int(p * g) for p in x]).tobytes()


def peak(a):
    x = array("h"); x.frombytes(a)
    if not x:
        return 0
    return max(max(x), -min(x))


def peak_db(p):
    import math
    return round(20 * math.log10(p / 32768), 1) if p > 0 else -90.0


async def kill_proc(p):
    if p is None:
        return
    try:
        if p.returncode is None:
            p.kill()
        await asyncio.wait_for(p.wait(), 3)
    except Exception:
        pass


# ======================================================================
#  Ascultători
# ======================================================================
class Listener:
    _ids = 0

    def __init__(self, req, station, hub, country):
        Listener._ids += 1
        self.id = Listener._ids
        self.req = req
        self.writer = req.writer
        self.ip = req.ip
        self.ua = req.headers.get("user-agent", "")
        self.country = country
        self.station = station
        self.hub = hub
        self.started = time.time()
        self.pending = collections.deque()
        self.pending_bytes = 0
        self.event = asyncio.Event()
        self.closed = False
        self.sent = 0
        self.metaint = 16000 if req.headers.get("icy-metadata") == "1" else 0
        self.meta_left = self.metaint
        self.sent_title = None
        self.session_id = None

    def push(self, data):
        if self.closed:
            return
        self.pending.append(data)
        self.pending_bytes += len(data)
        if self.pending_bytes > self.hub.max_backlog:
            self.close()          # client prea lent / blocat
        self.event.set()

    def close(self):
        if not self.closed:
            self.closed = True
            self.event.set()
            try:
                self.writer.close()
            except Exception:
                pass

    def _metablock(self):
        t = self.hub.station.title or ""
        if t == self.sent_title:
            return b"\x00"
        self.sent_title = t
        s = ("StreamTitle='%s';" % t.replace("'", "’")).encode("utf-8")[:4080]
        n = (len(s) + 15) // 16
        return bytes([n]) + s + b"\x00" * (n * 16 - len(s))

    def _with_meta(self, data):
        if not self.metaint:
            return data
        out = bytearray()
        while data:
            take = min(len(data), self.meta_left)
            out += data[:take]
            data = data[take:]
            self.meta_left -= take
            if self.meta_left == 0:
                out += self._metablock()
                self.meta_left = self.metaint
        return bytes(out)

    async def run(self):
        for b in list(self.hub.burst):
            self.push(b)
        while not self.closed:
            await self.event.wait()
            self.event.clear()
            if self.closed:
                break
            parts = []
            while self.pending:
                parts.append(self.pending.popleft())
            self.pending_bytes = 0
            if not parts:
                continue
            data = b"".join(parts)
            self.writer.write(self._with_meta(data))
            self.sent += len(data)
            await asyncio.wait_for(self.writer.drain(), 60)

    def info(self):
        dev, os_, player = classify_ua(self.ua)
        return {"id": self.id, "ip": self.ip, "ua": self.ua, "country": self.country, "mount": self.hub.mount,
                "station_id": self.station.id, "station": self.station.cfg["name"],
                "since": int(self.started), "duration": int(time.time() - self.started),
                "device": dev, "os": os_, "player": player, "sent": self.sent}


class Hub:
    """un mountpoint de ieșire (ex. /radio1 mp3 128k)"""

    def __init__(self, station, out):
        self.station = station
        self.codec = out["codec"]
        self.bitrate = int(out["bitrate"])
        self.mount = "/" + station.cfg["slug"] + (out.get("mount") or "")
        self.listeners = {}
        self.burst = collections.deque()
        self.burst_bytes = 0
        self.burst_max = self.bitrate * 125 * 3          # ~3 secunde pentru pornire rapidă
        self.max_backlog = self.bitrate * 125 * 30       # 30 s întârziere maximă la client
        self.bytes_out = 0

    @property
    def ctype(self):
        return "audio/mpeg" if self.codec == "mp3" else "audio/aac"

    def feed(self, data):
        self.burst.append(data)
        self.burst_bytes += len(data)
        while self.burst_bytes > self.burst_max and len(self.burst) > 1:
            self.burst_bytes -= len(self.burst.popleft())
        for l in list(self.listeners.values()):
            l.push(data)
        self.bytes_out += len(data) * len(self.listeners)


class Encoder:
    def __init__(self, station, hub):
        self.station = station
        self.hub = hub
        self.proc = None
        self.task = None
        self.restarts = 0

    async def start(self):
        args = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "s16le", "-ar", str(SR), "-ac", str(CH), "-i", "pipe:0"]
        if self.hub.codec == "mp3":
            args += ["-c:a", "libmp3lame", "-b:a", f"{self.hub.bitrate}k", "-write_xing", "0", "-f", "mp3"]
        else:
            args += ["-c:a", "aac", "-b:a", f"{self.hub.bitrate}k", "-f", "adts"]
        args += ["-flush_packets", "1", "pipe:1"]
        self.proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.DEVNULL)
        self.task = asyncio.create_task(self._read(self.proc))

    async def _read(self, proc):
        try:
            while True:
                data = await proc.stdout.read(8192)
                if not data:
                    break
                self.hub.feed(data)
        except Exception:
            pass

    async def write(self, chunk):
        p = self.proc
        if p is None or p.returncode is not None or p.stdin.is_closing():
            self.restarts += 1
            log(f"encoder {self.hub.mount} repornit", "warn", self.station.cfg["slug"])
            await self.stop()
            await self.start()
            p = self.proc
        try:
            p.stdin.write(chunk)
            await asyncio.wait_for(p.stdin.drain(), 2)
        except Exception:
            await kill_proc(p)

    async def stop(self):
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            await kill_proc(self.proc)
        if self.task:
            self.task.cancel()
        self.proc = None


# ======================================================================
#  Sursa live (studio)
# ======================================================================
class LiveInput:
    def __init__(self, station, ip, agent, ctype, proto, headers):
        self.station = station
        self.ip = ip
        self.agent = agent or "?"
        self.ctype = (ctype or "").lower()
        self.proto = proto
        self.name = headers.get("icy-name") or headers.get("ice-name") or ""
        self.bitrate = headers.get("icy-br") or headers.get("ice-bitrate") or ""
        if not self.bitrate and "ice-audio-info" in headers:
            for kv in headers["ice-audio-info"].split(";"):
                if kv.startswith(("bitrate=", "ice-bitrate=")):
                    self.bitrate = kv.split("=", 1)[1]
        self.connected_at = time.time()
        self.last_data = time.time()
        self.bytes_in = 0
        self.buf = bytearray()
        self.ready = False
        self.active = False
        self.silent_since = None
        self.silent = False
        self.loud_since = None
        self.proc = None
        self.task = None
        self.closed = False
        self.underruns = 0
        self.peak = 0
        self.kicked = False

    async def start(self):
        fmt = []
        if "mpeg" in self.ctype or "mp3" in self.ctype:
            fmt = ["-f", "mp3"]
        elif "aac" in self.ctype:
            fmt = ["-f", "aac"]
        elif "ogg" in self.ctype or "opus" in self.ctype:
            fmt = ["-f", "ogg"]
        args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-probesize", "65536", "-analyzeduration", "500000",
                *fmt, "-i", "pipe:0", "-vn", "-f", "s16le", "-ar", str(SR), "-ac", str(CH), "pipe:1"]
        self.proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.DEVNULL)
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        try:
            while True:
                data = await self.proc.stdout.read(16384)
                if not data:
                    break
                self.buf += data
                self.last_data = time.time()
                if len(self.buf) > BPS * 3:                 # limitează întârzierea la ~1 s
                    del self.buf[:len(self.buf) - BPS]
        except Exception:
            pass

    async def feed(self, data):
        self.bytes_in += len(data)
        p = self.proc
        if p is None or p.returncode is not None:
            raise ConnectionError("decoder oprit")
        p.stdin.write(data)
        await asyncio.wait_for(p.stdin.drain(), 10)

    def take(self, n):
        """None = sursa nu e utilizabilă (încă nu a pornit / a amuțit rețeaua)"""
        if time.time() - self.last_data > 4:
            self.ready = False
            self.active = False
            return None
        if not self.ready:
            if len(self.buf) >= BPS // 2:
                self.ready = True
                self.active = True
            elif self.active:
                return b"\x00" * n          # rebuffer scurt: nu sărim pe AutoDJ pentru o sughițare
            else:
                return None
        if len(self.buf) < n:
            self.underruns += 1
            self.ready = False
            data = bytes(self.buf) + b"\x00" * (n - len(self.buf))
            self.buf.clear()
            return data
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    def usable(self, silence_secs):
        if not self.active or self.closed:
            return False
        if silence_secs and self.silent:
            return False
        return True

    def update_silence(self, p, silence_secs):
        self.peak = p
        t = time.time()
        if not silence_secs:
            self.silent = False
            return
        if p < SILENT_PEAK:
            self.loud_since = None
            if self.silent_since is None:
                self.silent_since = t
            if not self.silent and t - self.silent_since >= silence_secs:
                self.silent = True
                log("liniște pe sursa live -> AutoDJ", "warn", self.station.cfg["slug"])
                self.station.radio.db.event(self.station.id, "silence", f"liniște > {silence_secs}s")
                self.station.radio.alert(f"⚠️ {self.station.cfg['name']}: liniște pe emisia din studio "
                                         f"(>{silence_secs}s). Am trecut pe AutoDJ.")
        else:
            self.silent_since = None
            if self.silent:
                if self.loud_since is None:
                    self.loud_since = t
                if t - self.loud_since > 2:
                    self.silent = False
                    log("sunetul a revenit pe sursa live", "info", self.station.cfg["slug"])

    async def close(self):
        self.closed = True
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            await kill_proc(self.proc)
        if self.task:
            self.task.cancel()

    def info(self):
        return {"ip": self.ip, "agent": self.agent, "proto": self.proto, "ctype": self.ctype, "name": self.name,
                "bitrate": self.bitrate, "since": int(self.connected_at), "bytes_in": self.bytes_in,
                "ready": self.ready, "silent": self.silent, "underruns": self.underruns,
                "peak_db": peak_db(self.peak)}


# ======================================================================
#  AutoDJ
# ======================================================================
class Track:
    def __init__(self, path, jingle=False):
        self.path = path
        self.jingle = jingle
        self.title = os.path.splitext(os.path.basename(path))[0].replace("_", " ")
        self.duration = 0.0
        self.buf = bytearray()
        self.eof = False
        self.consumed = 0
        self.proc = None
        self.task = None
        self.announced = False

    async def open(self):
        try:
            p = await asyncio.create_subprocess_exec(FFPROBE, "-v", "quiet", "-print_format", "json",
                                                     "-show_format", self.path, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 15)
            fmt = json.loads(out or b"{}").get("format", {})
            self.duration = float(fmt.get("duration") or 0)
            tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
            artist, title = tags.get("artist", "").strip(), tags.get("title", "").strip()
            if title:
                self.title = f"{artist} - {title}" if artist else title
        except Exception:
            pass
        self.proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", self.path, "-vn",
            "-f", "s16le", "-ar", str(SR), "-ac", str(CH), "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        self.task = asyncio.create_task(self._read())
        # așteaptă primele date (max 5 s) ca să știm că fișierul e valid
        for _ in range(100):
            if len(self.buf) >= CHUNK or self.eof:
                break
            await asyncio.sleep(0.05)
        return len(self.buf) > 0

    async def _read(self):
        try:
            while True:
                if len(self.buf) > BPS * 4:
                    await asyncio.sleep(0.1)
                    continue
                data = await self.proc.stdout.read(65536)
                if not data:
                    break
                self.buf += data
        except Exception:
            pass
        finally:
            self.eof = True

    @property
    def remaining(self):
        if not self.duration:
            return None
        return self.duration * BPS - self.consumed

    def finished(self):
        return self.eof and not self.buf

    def take(self, n):
        data = bytes(self.buf[:n])
        del self.buf[:n]
        self.consumed += len(data)
        return data

    async def close(self):
        await kill_proc(self.proc)
        if self.task:
            self.task.cancel()


class AutoDJ:
    def __init__(self, station):
        self.station = station
        self.cur = None
        self.next = None
        self.preparing = False
        self.queue = []
        self.recent = collections.deque(maxlen=20)
        self.since_jingle = 0
        self.scan_time = 0
        self.files = []
        self.jingles = []
        self.fade_in = 0

    @property
    def cfg(self):
        return self.station.cfg

    def dirs(self):
        base = os.path.join(self.station.radio.data_dir, "stations", self.cfg["slug"])
        return os.path.join(base, "music"), os.path.join(base, "jingles")

    def scan(self, force=False):
        if not force and time.time() - self.scan_time < 30:
            return
        self.scan_time = time.time()
        md, jd = self.dirs()
        os.makedirs(md, exist_ok=True)
        os.makedirs(jd, exist_ok=True)

        def walk(d):
            res = []
            for root, _, files in os.walk(d):
                for f in files:
                    if os.path.splitext(f)[1].lower() in AUDIO_EXT:
                        res.append(os.path.join(root, f))
            return sorted(res)
        files = walk(md)
        if files != self.files:
            self.files = files
            self.queue = [f for f in self.queue if f in files]
        self.jingles = walk(jd)

    def has_content(self):
        self.scan()
        return bool(self.files or self.jingles)

    def pick(self):
        self.scan()
        je = int(self.cfg.get("jingle_every") or 0)
        if self.jingles and (not self.files or (je and self.since_jingle >= je)):
            self.since_jingle = 0
            return random.choice(self.jingles), True
        if not self.files:
            return None, False
        if not self.queue:
            q = list(self.files)
            if self.cfg.get("shuffle", 1):
                random.shuffle(q)
                if len(q) > 3:  # evită repetarea imediată
                    recent = set(self.recent)
                    q.sort(key=lambda f: f in recent)
            self.queue = q
        self.since_jingle += 1
        f = self.queue.pop(0)
        self.recent.append(f)
        return f, False

    async def prepare(self):
        if self.preparing or self.next is not None:
            return
        self.preparing = True
        try:
            for _ in range(5):
                path, jingle = self.pick()
                if not path:
                    return
                t = Track(path, jingle)
                ok = await t.open()
                if ok:
                    self.next = t
                    return
                await t.close()
                log(f"fișier invalid sărit: {os.path.basename(path)}", "warn", self.cfg["slug"])
        finally:
            self.preparing = False

    def _promote(self):
        self.cur = self.next
        self.next = None
        if self.cur and not self.cur.announced:
            self.cur.announced = True
            if not self.cur.jingle:
                self.station.on_track(self.cur.title, "autodj")
        asyncio.create_task(self.prepare())

    async def skip(self):
        old = self.cur
        self.cur = None
        if old:
            asyncio.create_task(old.close())
        if self.next:
            self._promote()
        else:
            asyncio.create_task(self.prepare())
        self.fade_in = 10

    async def restart_fresh(self):
        """după o emisie live lungă: începe o piesă nouă în loc să reia piesa întreruptă"""
        await self.skip()

    def pull(self, n):
        """întoarce exact n bytes PCM"""
        out = bytearray()
        guard = 0
        while len(out) < n and guard < 4:
            guard += 1
            if self.cur is None or self.cur.finished():
                if self.cur is not None:
                    asyncio.create_task(self.cur.close())
                    self.cur = None
                if self.next is not None:
                    self._promote()
                else:
                    if not self.preparing:
                        asyncio.create_task(self.prepare())
                    break
            cur = self.cur
            need = n - len(out)
            xf = float(self.cfg.get("crossfade") or 0)
            if cur.jingle or (self.next and self.next.jingle):
                xf = min(xf, 1.0)
            xfb = int(xf * BPS)
            rem = cur.remaining
            if xfb > 0 and self.next is not None and rem is not None and rem <= xfb and len(self.next.buf) >= need:
                a = cur.take(need)
                a += b"\x00" * (need - len(a))
                g = min(1.0, (self.next.consumed + need) / xfb)
                b = self.next.take(need)
                b += b"\x00" * (need - len(b))
                if not self.next.announced:
                    self.next.announced = True
                    if not self.next.jingle:
                        self.station.on_track(self.next.title, "autodj")
                out += mix(a, b, 1.0 - g, g)
                if cur.finished() or rem - need <= 0:
                    asyncio.create_task(cur.close())
                    self.cur = None
                    self._promote()
                continue
            if not cur.buf and not cur.eof:
                break   # decoder lent - completăm cu liniște
            out += cur.take(need)
        if len(out) < n:
            out += b"\x00" * (n - len(out))
        data = bytes(out)
        if self.fade_in > 0:
            data = gain(data, 1 - self.fade_in / 10)
            self.fade_in -= 1
        return data

    async def stop(self):
        for t in (self.cur, self.next):
            if t:
                await t.close()
        self.cur = self.next = None

    def info(self):
        self.scan()
        return {"files": len(self.files), "jingles": len(self.jingles),
                "current": self.cur.title if self.cur else None,
                "current_pos": round(self.cur.consumed / BPS) if self.cur else 0,
                "current_dur": round(self.cur.duration) if self.cur else 0,
                "next": self.next.title if self.next else None}


# ======================================================================
#  Post de radio
# ======================================================================
class Station:
    def __init__(self, radio, cfg):
        self.radio = radio
        self.cfg = cfg
        self.id = cfg["id"]
        self.hubs = {}
        self.encoders = []
        self.autodj = AutoDJ(self)
        self.live = None
        self.mode = "off"
        self.title = cfg["name"]
        self.live_title = None
        self.task = None
        self.running = False
        self.out_peak = 0
        self.live_started = None
        self.peak_today = (time.strftime("%Y-%m-%d"), 0)
        self.alert_task = None
        self.alerted_down = False

    @property
    def slug(self):
        return self.cfg["slug"]

    def listener_count(self):
        return sum(len(h.listeners) for h in self.hubs.values())

    async def start(self):
        self.hubs = {}
        self.encoders = []
        for out in self.cfg["outputs"]:
            h = Hub(self, out)
            self.hubs[h.mount] = h
            e = Encoder(self, h)
            await e.start()
            self.encoders.append(e)
        self.running = True
        self.autodj.scan(force=True)
        if self.cfg.get("autodj", 1):
            await self.autodj.prepare()
        self.task = asyncio.create_task(self.run())
        log(f"pornit, mounturi: {', '.join(self.hubs)}", "info", self.slug)

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
        for h in self.hubs.values():
            for l in list(h.listeners.values()):
                l.close()
        for e in self.encoders:
            await e.stop()
        await self.autodj.stop()
        if self.live:
            self.live.kicked = True
            await self.live.close()
            self.live = None
        log("oprit", "info", self.slug)

    def on_track(self, title, source):
        title = (title or "").strip()
        if not title or title == self.title:
            return
        self.title = title
        try:
            self.radio.db.exec("INSERT INTO songs(station_id, ts, title, source, listeners) VALUES(?,?,?,?,?)",
                               (self.id, now(), title, source, self.listener_count()))
        except Exception as e:
            log(f"db songs: {e}", "error", self.slug)

    def set_mode(self, mode):
        if mode == self.mode:
            return
        old = self.mode
        self.mode = mode
        log(f"sursă: {old} -> {mode}", "info", self.slug)
        if mode == "live":
            self.live_started = time.time()
            self.on_track(self.live_title or f"{self.cfg['name']} - LIVE", "live")
        elif mode == "autodj":
            if old == "live" and self.live_started and time.time() - self.live_started > 30:
                asyncio.create_task(self.autodj.restart_fresh())
            elif self.autodj.cur and not self.autodj.cur.jingle:
                self.on_track(self.autodj.cur.title, "autodj")
            self.autodj.fade_in = 10
        elif mode == "silence":
            self.on_track(self.cfg["name"], "off")

    async def run(self):
        silence = b"\x00" * CHUNK
        fade = 0
        t_next = time.monotonic()
        while self.running:
            try:
                t_next += CHUNK_MS / 1000
                delay = t_next - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -1.0:          # am rămas mult în urmă (sistem încărcat) -> resincronizare
                    t_next = time.monotonic()
                silence_secs = int(self.cfg.get("silence_secs") or 0)
                data = None
                live = self.live
                if live:
                    ld = live.take(CHUNK)
                    if ld is not None:
                        live.update_silence(peak(ld), silence_secs)
                        if live.usable(silence_secs):
                            data = ld
                if data is not None:
                    mode = "live"
                elif self.cfg.get("autodj", 1) and self.autodj.has_content():
                    mode = "autodj"
                    data = self.autodj.pull(CHUNK)
                else:
                    mode = "silence"
                    data = silence
                if mode != self.mode:
                    self.set_mode(mode)
                    fade = 10
                if fade > 0 and mode == "live":
                    data = gain(data, 1 - fade / 10)
                if fade > 0:
                    fade -= 1
                self.out_peak = max(int(self.out_peak * 0.8), peak(data))
                for e in self.encoders:
                    await e.write(data)
            except asyncio.CancelledError:
                break
            except Exception as ex:
                log(f"mixer: {ex!r}", "error", self.slug)
                await asyncio.sleep(0.2)

    # ---------- sursă live ----------
    async def attach_live(self, live):
        if self.live:
            old = self.live
            if time.time() - old.last_data < 3:
                return False, "Sursa este deja conectată"
            old.kicked = True
            await old.close()
        await live.start()
        self.live = live
        self.live_title = None
        self.radio.db.event(self.id, "source_connect", f"{live.ip} {live.agent} ({live.proto})")
        log(f"studio conectat: {live.ip} [{live.agent}] {live.proto}", "info", self.slug)
        if self.alert_task:
            self.alert_task.cancel()
            self.alert_task = None
        if self.alerted_down:
            self.alerted_down = False
            self.radio.alert(f"🟢 {self.cfg['name']}: studioul s-a reconectat ({live.ip}).")
        return True, ""

    async def detach_live(self, live):
        if self.live is not live:
            await live.close()
            return
        self.live = None
        await live.close()
        dur = int(time.time() - live.connected_at)
        self.radio.db.event(self.id, "source_disconnect", f"{live.ip} după {dur}s")
        log(f"studio deconectat ({dur}s)", "warn", self.slug)
        if not live.kicked:
            self.alert_task = asyncio.create_task(self._alert_down())

    async def _alert_down(self):
        await asyncio.sleep(20)
        if self.live is None:
            self.alerted_down = True
            self.radio.alert(f"🔴 {self.cfg['name']}: studioul s-a deconectat. Emite AutoDJ." if
                             self.cfg.get("autodj", 1) else f"🔴 {self.cfg['name']}: studioul s-a deconectat!")

    def set_live_title(self, title):
        self.live_title = title
        if self.mode == "live":
            self.on_track(title, "live")

    def status(self):
        st = {"id": self.id, "slug": self.slug, "name": self.cfg["name"], "mode": self.mode, "title": self.title,
              "listeners": self.listener_count(), "max_listeners": self.cfg.get("max_listeners"),
              "mounts": [{"mount": h.mount, "codec": h.codec, "bitrate": h.bitrate, "listeners": len(h.listeners)}
                         for h in self.hubs.values()],
              "live": self.live.info() if self.live else None,
              "autodj": self.autodj.info() if self.cfg.get("autodj", 1) else None,
              "out_peak_db": peak_db(self.out_peak), "running": self.running,
              "peak_today": self.peak_today[1]}
        return st

    def tick_peak(self):
        d = time.strftime("%Y-%m-%d")
        c = self.listener_count()
        if self.peak_today[0] != d:
            self.peak_today = (d, c)
        elif c > self.peak_today[1]:
            self.peak_today = (d, c)
