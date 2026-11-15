"""Relay de intrare (preia un stream extern) și relay de ieșire (retransmite postul către alte servere)."""
import asyncio
import base64
import collections
import datetime as dt
import json
import os
import re
import ssl
import time
import urllib.parse

from .util import log, decode_text

UA = "NtS-Radio/1.0"


# ======================================================================
#  utilitare
# ======================================================================
def schedule_active(schedule, tz):
    """schedule: [{days:[1..7], start:"HH:MM", end:"HH:MM"}]; listă goală = mereu activ"""
    if not schedule:
        return True
    now = dt.datetime.now(tz)
    for s in schedule:
        try:
            days = {int(d) for d in s.get("days") or []}
            h1, m1 = map(int, s["start"].split(":"))
            h2, m2 = map(int, s["end"].split(":"))
        except Exception:
            continue
        a, b, cur = h1 * 60 + m1, h2 * 60 + m2, now.hour * 60 + now.minute
        wd = now.isoweekday()
        if a < b:
            if wd in days and a <= cur < b:
                return True
        else:  # trece de miezul nopții
            yday = 7 if wd == 1 else wd - 1
            if (wd in days and cur >= a) or (yday in days and cur < b):
                return True
    return False


async def http_open(url, extra_headers=None, timeout=15, max_redirects=5):
    """GET minimal (HTTP/1.0, ICY acceptat). Întoarce (reader, writer, code, headers, url_final)"""
    for _ in range(max_redirects + 1):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https"):
            raise ValueError("doar http:// sau https://")
        tls = u.scheme == "https"
        port = u.port or (443 if tls else 80)
        ctx = ssl.create_default_context() if tls else None
        r, w = await asyncio.wait_for(asyncio.open_connection(u.hostname, port, ssl=ctx,
                                                              server_hostname=u.hostname if tls else None), timeout)
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        host = u.hostname + (f":{u.port}" if u.port else "")
        h = [f"GET {path} HTTP/1.0", f"Host: {host}", f"User-Agent: {UA}", "Accept: */*", "Connection: close"]
        if u.username:
            cred = f"{urllib.parse.unquote(u.username)}:{urllib.parse.unquote(u.password or '')}"
            h.append("Authorization: Basic " + base64.b64encode(cred.encode()).decode())
        for k, v in (extra_headers or {}).items():
            h.append(f"{k}: {v}")
        w.write(("\r\n".join(h) + "\r\n\r\n").encode())
        await w.drain()
        line = await asyncio.wait_for(r.readline(), timeout)
        parts = line.decode("latin-1").split(" ", 2)
        try:
            code = int(parts[1])
        except Exception:
            w.close()
            raise ConnectionError("răspuns invalid de la server")
        headers = {}
        for _ in range(100):
            hl = await asyncio.wait_for(r.readline(), timeout)
            if hl in (b"\r\n", b"\n", b""):
                break
            if b":" in hl:
                k, v = hl.decode("latin-1").split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if code in (301, 302, 303, 307, 308) and "location" in headers:
            w.close()
            url = urllib.parse.urljoin(url, headers["location"])
            continue
        return r, w, code, headers, url
    raise ConnectionError("prea multe redirecționări")


async def http_get_quick(url, timeout=10):
    try:
        r, w, code, _, _ = await http_open(url, timeout=timeout)
        try:
            await asyncio.wait_for(r.read(4096), timeout)
        except Exception:
            pass
        w.close()
        return code
    except Exception as e:
        return str(e)


def first_url_in_playlist(text):
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("file") and "=" in line:
            line = line.split("=", 1)[1].strip()
        if line.startswith(("http://", "https://")):
            return line
    return None


# ======================================================================
#  Relay de intrare
# ======================================================================
async def relay_loop(station):
    """rulează cât timp postul e pornit; se conectează la sursa externă când relay-ul e activ și în program"""
    from .engine import LiveInput
    st = station
    backoff = 3
    st._relay_down_since, st._relay_alerted = None, False
    while st.running:
        rc = st.cfg.get("relay") or {}
        if not rc.get("enabled") or not rc.get("url"):
            st.relay_status = {"state": "off"}
            st._relay_down_since, st._relay_alerted = None, False
            await asyncio.sleep(3)
            continue
        if not schedule_active(rc.get("schedule"), st.radio.tz()):
            st.relay_status = {"state": "schedule", "url": rc["url"]}
            st._relay_down_since, st._relay_alerted = None, False
            await asyncio.sleep(5)
            continue
        url = rc["url"]
        st.relay_status = {"state": "connecting", "url": url}
        try:
            await _relay_once(st, url, LiveInput, rc)
            err = None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            err = str(e) or e.__class__.__name__
        finally:
            if st.relay:
                r = st.relay
                st.relay = None
                await r.close()
        if not st.running:
            break
        if err is None:              # oprit intenționat (dezactivat / în afara programului / URL schimbat)
            backoff = 3
            continue
        if st.relay_status.get("state") == "connected" or st._relay_down_since is None:
            if st._relay_down_since is None:
                st._relay_down_since = time.time()
            st.radio.db.event(st.id, "relay_down", f"{url}: {err}")
            log(f"relay întrerupt: {err}", "warn", st.slug)
            backoff = 3
        elif not st._relay_alerted and time.time() - st._relay_down_since > 30:
            st._relay_alerted = True
            st.radio.alert(f"🔴 {st.cfg['name']}: relay-ul de intrare nu răspunde ({err}).")
        st.relay_status = {"state": "error", "url": url, "error": err[:200], "retry_in": backoff}
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)
    st.relay_status = {"state": "off"}


async def _relay_once(st, url, LiveInput, rc):
    # rezolvă playlist-uri (.pls/.m3u) și detectează HLS
    hls = False
    for _ in range(4):
        if re.search(r"\.m3u8(\?|$)", url, re.I):
            hls = True
            break
        r, w, code, headers, url = await http_open(url, {"Icy-MetaData": "1"})
        ctype = headers.get("content-type", "").lower()
        if code != 200:
            w.close()
            raise ConnectionError(f"HTTP {code}")
        is_pl = any(x in ctype for x in ("mpegurl", "scpls", "x-pls", "text/")) or \
            re.search(r"\.(pls|m3u)(\?|$)", urllib.parse.urlsplit(url).path, re.I)
        if is_pl and "audio/mpeg" not in ctype and "aac" not in ctype:
            body = await asyncio.wait_for(r.read(65536), 15)
            w.close()
            text = body.decode("utf-8", "replace")
            if "#EXT-X-" in text:
                hls = True
                break
            nxt = first_url_in_playlist(text)
            if not nxt:
                raise ConnectionError("playlist fără adrese de stream")
            url = urllib.parse.urljoin(url, nxt)
            continue
        break
    else:
        raise ConnectionError("prea multe playlist-uri înlănțuite")

    if hls:
        live = LiveInput(st, urllib.parse.urlsplit(url).hostname, "HLS", "", "Relay", {})
        live.source_url = url
        await live.start_url(url)
        st.relay = live
        st.relay_title = None
        _mark_connected(st, url, "HLS")
        while st.running and live.proc.returncode is None:
            await asyncio.sleep(1)
            rc2 = st.cfg.get("relay") or {}
            if not rc2.get("enabled") or rc2.get("url") != rc.get("url") or \
                    not schedule_active(rc2.get("schedule"), st.radio.tz()):
                return
        raise ConnectionError("stream HLS întrerupt")

    live = LiveInput(st, urllib.parse.urlsplit(url).hostname, headers.get("server") or headers.get("icy-name") or "",
                     headers.get("content-type", ""), "Relay", headers)
    live.source_url = url
    await live.start()
    st.relay = live
    st.relay_title = None
    _mark_connected(st, url, headers.get("content-type", ""))
    metaint = int(headers.get("icy-metaint") or 0)
    left = metaint
    last_check = time.time()
    try:
        while st.running and not live.closed:
            if metaint:
                data = await asyncio.wait_for(r.read(min(16384, left)), 20)
                if not data:
                    break
                left -= len(data)
                await live.feed(data)
                if left == 0:
                    n = (await asyncio.wait_for(r.readexactly(1), 20))[0] * 16
                    if n:
                        meta = await asyncio.wait_for(r.readexactly(n), 20)
                        m = re.search(rb"StreamTitle='(.*?)';", meta, re.S)
                        if m:
                            st.set_relay_title(decode_text(m.group(1)).strip())
                    left = metaint
            else:
                data = await asyncio.wait_for(r.read(16384), 20)
                if not data:
                    break
                await live.feed(data)
            if time.time() - last_check > 2:
                last_check = time.time()
                rc2 = st.cfg.get("relay") or {}
                if not rc2.get("enabled") or rc2.get("url") != rc.get("url") or \
                        not schedule_active(rc2.get("schedule"), st.radio.tz()):
                    return
    finally:
        w.close()
    raise ConnectionError("sursa a închis conexiunea")


def _mark_connected(st, url, ctype):
    st.relay_status = {"state": "connected", "url": url, "since": int(time.time()), "ctype": ctype}
    st.radio.db.event(st.id, "relay_up", url)
    log(f"relay conectat: {url}", "info", st.slug)
    if getattr(st, "_relay_alerted", False):
        st.radio.alert(f"🟢 {st.cfg['name']}: relay-ul de intrare funcționează din nou.")
    st._relay_down_since, st._relay_alerted = None, False


# ======================================================================
#  Relay de ieșire
# ======================================================================
class Tap:
    """consumator intern al unui mount (nu se numără ca ascultător)"""

    def __init__(self, max_bytes):
        self.q = collections.deque()
        self.bytes = 0
        self.max = max_bytes
        self.event = asyncio.Event()
        self.overflow = False

    def push(self, data):
        self.q.append(data)
        self.bytes += len(data)
        if self.bytes > self.max:
            self.overflow = True
        self.event.set()

    async def get(self):
        while not self.q:
            self.event.clear()
            await self.event.wait()
        if self.overflow:
            raise ConnectionError("serverul destinație nu primește date destul de repede")
        parts = list(self.q)
        self.q.clear()
        self.bytes = 0
        return b"".join(parts)


class Push:
    TYPES = ("icecast", "shoutcast", "rtmp")

    def __init__(self, station, cfg):
        self.station = station
        self.cfg = cfg
        self.key = json.dumps(cfg, sort_keys=True)
        self.state = "off"
        self.error = ""
        self.since = None
        self.bytes = 0
        self.task = None
        self.writer = None
        self.proc = None
        self.tap = None
        self.hub = None

    @property
    def name(self):
        return self.cfg.get("name") or self.cfg.get("host") or self.cfg.get("url", "")[:40]

    def pick_hub(self):
        hubs = list(self.station.hubs.values())
        want = "/" + self.station.slug + (self.cfg.get("output") or "")
        for h in hubs:
            if h.mount == want:
                return h
        return hubs[0] if hubs else None

    def start(self):
        if self.cfg.get("enabled", 1):
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except BaseException:
                pass
        await self._cleanup()
        self.state = "off"

    async def _cleanup(self):
        if self.hub and self.tap:
            self.hub.taps.discard(self.tap)
        self.tap = None
        if self.writer:
            try:
                self.writer.close()
            except Exception:
                pass
            self.writer = None
        if self.proc:
            try:
                self.proc.kill()
                await asyncio.wait_for(self.proc.wait(), 3)
            except Exception:
                pass
            self.proc = None

    async def run(self):
        backoff = 3
        while True:
            self.state = "connecting"
            try:
                await self.once()
                err = "conexiune închisă de server"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                err = str(e) or e.__class__.__name__
            finally:
                await self._cleanup()
            if self.state == "connected" or self.since:
                self.station.radio.db.event(self.station.id, "push_down", f"{self.name}: {err}")
                log(f"retransmisie {self.name} întreruptă: {err}", "warn", self.station.slug)
                backoff = 3
            self.state, self.error, self.since = "error", err[:300], None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _attach(self):
        self.hub = self.pick_hub()
        if not self.hub:
            raise ConnectionError("postul nu are ieșiri")
        self.tap = Tap(self.hub.bitrate * 125 * 20)
        self.hub.taps.add(self.tap)

    def _connected(self):
        self.state, self.error, self.since = "connected", "", int(time.time())
        self.station.radio.db.event(self.station.id, "push_up", self.name)
        log(f"retransmisie {self.name} conectată", "info", self.station.slug)
        self.notify_title(self.station.title)

    async def once(self):
        t = self.cfg.get("type")
        if t == "rtmp":
            return await self._rtmp()
        c = self.cfg
        host, port = c.get("host", "").strip(), int(c.get("port") or 8000)
        if not host:
            raise ValueError("lipsește serverul")
        hub = self.pick_hub()
        ctype = "audio/mpeg" if hub.codec == "mp3" else "audio/aac"
        scfg = self.station.cfg
        tls = bool(c.get("tls"))
        ctx = ssl.create_default_context() if tls else None
        if t == "icecast":
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx,
                                                                  server_hostname=host if tls else None), 15)
            self.writer = w
            mount = "/" + (c.get("mount") or "stream").lstrip("/")
            cred = base64.b64encode(f"{c.get('user') or 'source'}:{c.get('password', '')}".encode()).decode()
            h = [f"PUT {mount} HTTP/1.1", f"Host: {host}:{port}", f"Authorization: Basic {cred}",
                 f"User-Agent: {UA}", f"Content-Type: {ctype}", "Ice-Public: 0",
                 f"Ice-Name: {scfg['name']}", f"Ice-Description: {scfg.get('description') or ''}",
                 f"Ice-Genre: {scfg.get('genre') or ''}", f"Ice-Url: {scfg.get('website') or ''}",
                 f"Ice-Bitrate: {hub.bitrate}", f"Ice-Audio-Info: bitrate={hub.bitrate}"]
            w.write(("\r\n".join(h) + "\r\n\r\n").encode("utf-8", "replace"))
            await w.drain()
            line = (await asyncio.wait_for(r.readline(), 10)).decode("latin-1").strip()
            code = int(line.split(" ")[1]) if len(line.split(" ")) > 1 and line.split(" ")[1].isdigit() else 0
            if code not in (100, 200):
                raise ConnectionError({401: "parolă / utilizator greșit (401)", 403: "mount ocupat sau interzis (403)",
                                       404: "mount inexistent (404)"}.get(code, f"respins: {line or 'fără răspuns'}"))
        elif t == "shoutcast":
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port + 1), 15)
            self.writer = w
            w.write((c.get("password", "") + "\r\n").encode())
            await w.drain()
            line = (await asyncio.wait_for(r.readline(), 10)).decode("latin-1").strip()
            if not line.upper().startswith("OK"):
                raise ConnectionError(f"respins: {line or 'fără răspuns'} (verifică parola)")
            h = [f"icy-name:{scfg['name']}", f"icy-genre:{scfg.get('genre') or ''}", "icy-pub:0",
                 f"icy-br:{hub.bitrate}", f"icy-url:{scfg.get('website') or ''}", f"content-type:{ctype}"]
            w.write(("\r\n".join(h) + "\r\n\r\n").encode("utf-8", "replace"))
            await w.drain()
        else:
            raise ValueError("tip necunoscut")
        self._attach()
        self._connected()
        while True:
            data = await self.tap.get()
            w.write(data)
            await asyncio.wait_for(w.drain(), 30)
            self.bytes += len(data)

    async def _rtmp(self):
        c = self.cfg
        url = (c.get("url") or "").strip()
        if not url.startswith(("rtmp://", "rtmps://")):
            raise ValueError("adresa trebuie să înceapă cu rtmp:// sau rtmps://")
        key = (c.get("key") or "").strip()
        if key:
            url = url.rstrip("/") + "/" + key
        hub = self.pick_hub()
        from .engine import FFMPEG
        args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-thread_queue_size", "1024",
                "-f", "mp3" if hub.codec == "mp3" else "aac", "-i", "pipe:0"]
        if c.get("video", 1):
            logo = os.path.join(self.station.radio.data_dir, "stations", self.station.slug, "logo")
            ok_logo = False
            if os.path.isfile(logo):
                with open(logo, "rb") as f:
                    head = f.read(16)
                ok_logo = head[:3] == b"\xff\xd8\xff" or head[:4] in (b"\x89PNG", b"RIFF")
            if ok_logo:
                args += ["-re", "-loop", "1", "-framerate", "15", "-i", logo]
            else:
                args += ["-re", "-f", "lavfi", "-i", "color=c=0x1c5cab:s=1280x720:r=15"]
            args += ["-map", "0:a", "-map", "1:v", "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
                     "-pix_fmt", "yuv420p", "-r", "15", "-g", "30", "-b:v", "500k", "-maxrate", "500k",
                     "-bufsize", "1000k", "-vf",
                     "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=0x111111"]
        else:
            args += ["-map", "0:a"]
        args += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-f", "flv", url]
        self.proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.DEVNULL,
                                                         stderr=asyncio.subprocess.PIPE)
        errbuf = collections.deque(maxlen=5)

        async def read_err(p):
            try:
                while True:
                    l = await p.stderr.readline()
                    if not l:
                        break
                    errbuf.append(l.decode("utf-8", "replace").strip())
            except Exception:
                pass
        et = asyncio.create_task(read_err(self.proc))
        self._attach()
        started = time.time()
        try:
            while True:
                if self.proc.returncode is not None:
                    break
                try:
                    data = await asyncio.wait_for(self.tap.get(), 5)
                except asyncio.TimeoutError:
                    continue
                self.proc.stdin.write(data)
                await asyncio.wait_for(self.proc.stdin.drain(), 20)
                self.bytes += len(data)
                if self.state != "connected" and time.time() - started > 4 and self.proc.returncode is None:
                    self._connected()
        except (BrokenPipeError, ConnectionResetError):
            pass
        await asyncio.sleep(0.3)
        et.cancel()
        raise ConnectionError(" | ".join(errbuf) or "ffmpeg s-a oprit")

    def notify_title(self, title):
        c = self.cfg
        if self.state != "connected" or c.get("type") == "rtmp" or not title:
            return
        host, port = c.get("host"), int(c.get("port") or 8000)
        proto = "https" if c.get("tls") else "http"
        q = urllib.parse.quote(title)
        if c.get("type") == "icecast":
            mount = urllib.parse.quote("/" + (c.get("mount") or "stream").lstrip("/"))
            user = urllib.parse.quote(c.get("user") or "source")
            pw = urllib.parse.quote(c.get("password", ""), safe="")
            url = f"{proto}://{user}:{pw}@{host}:{port}/admin/metadata?mount={mount}&mode=updinfo&song={q}"
        else:
            pw = urllib.parse.quote(c.get("password", ""), safe="")
            url = f"http://{host}:{port}/admin.cgi?pass={pw}&mode=updinfo&song={q}"
        asyncio.create_task(http_get_quick(url))

    def info(self):
        return {"name": self.name, "type": self.cfg.get("type"), "state": self.state, "error": self.error,
                "since": self.since, "bytes": self.bytes, "enabled": bool(self.cfg.get("enabled", 1)),
                "target": self.cfg.get("url") if self.cfg.get("type") == "rtmp" else
                f"{self.cfg.get('host')}:{self.cfg.get('port')}" +
                (("/" + (self.cfg.get("mount") or "stream").lstrip("/")) if self.cfg.get("type") == "icecast" else "")}
