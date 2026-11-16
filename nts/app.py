import asyncio
import base64
import csv
import io
import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from . import VERSION, stats
from .db import DB, DEFAULT_OUTPUTS
from .engine import Station, LiveInput, Listener
from .geo import Geo
from .httpd import HTTPError, Server, send, send_json, send_file, ChunkedReader
from .util import LOG, log, now, hash_password, check_password, gen_password, slugify, safe_filename, decode_text

SESSION_COOKIE = "nts_sess"
SESSION_TTL = 30 * 86400


class Radio:
    def __init__(self, data_dir, web_dir, host="0.0.0.0", port=8000):
        self.data_dir = data_dir
        self.web_dir = web_dir
        self.host = host
        self.port = port
        os.makedirs(data_dir, exist_ok=True)
        self.db = DB(os.path.join(data_dir, "nts-radio.db"))
        self.geo = Geo(data_dir)
        self.stations = {}          # id -> Station
        self.started = time.time()
        self.login_fail = {}
        self.src_fails = {}
        self.cpu_prev = None

    # ------------------------------------------------------------------ pornire
    async def start(self):
        self.db.close_dangling_sessions()
        self.first_run()
        for cfg in self.db.stations():
            if cfg["enabled"]:
                await self.start_station(cfg)
        srv = Server(self, self.host, self.port)
        await srv.start()
        sc = await asyncio.start_server(self.handle_sc1, self.host, self.port + 1)
        log(f"NtS-Radio {VERSION} pornit: http://{self.host}:{self.port}  (sursă SHOUTcast v1 pe portul {self.port + 1})")
        self.geo.update_async()
        asyncio.create_task(self.snapshot_loop())
        asyncio.create_task(self.housekeeping_loop())
        await asyncio.gather(srv.server.serve_forever(), sc.serve_forever())

    def first_run(self):
        if not self.db.one("SELECT id FROM users WHERE role='admin'"):
            pw = os.environ.get("NTS_ADMIN_PASS") or gen_password(12)
            self.db.exec("INSERT INTO users(username, pass, role, created) VALUES(?,?,?,?)",
                         ("admin", hash_password(pw), "admin", now()))
            p = os.path.join(self.data_dir, "admin-password.txt")
            with open(p, "w") as f:
                f.write(f"utilizator: admin\nparola: {pw}\n")
            os.chmod(p, 0o600)
            log(f"cont admin creat -> utilizator: admin  parola: {pw}")
        if not self.db.one("SELECT id FROM stations"):
            self.db.exec("INSERT INTO stations(slug, name, source_pass, outputs, created) VALUES(?,?,?,?,?)",
                         ("radio", "Radio", gen_password(10), json.dumps(DEFAULT_OUTPUTS), now()))
            log("post implicit creat: /radio")

    async def start_station(self, cfg):
        st = Station(self, cfg)
        r = self.db.one("SELECT MAX(listeners) AS m FROM snapshots WHERE station_id=? AND ts>=?",
                        (cfg["id"], self.day_start()))
        st.peak_today = (time.strftime("%Y-%m-%d"), (r or {}).get("m") or 0)
        self.stations[cfg["id"]] = st
        await st.start()
        return st

    async def stop_station(self, sid):
        st = self.stations.pop(sid, None)
        if st:
            await st.stop()

    def tz(self):
        try:
            return ZoneInfo(self.db.get_setting("timezone", "Europe/Bucharest"))
        except Exception:
            return ZoneInfo("Europe/Bucharest")

    def tzname(self):
        return self.db.get_setting("timezone", "Europe/Bucharest")

    def day_start(self):
        import datetime as dt
        d = dt.datetime.now(self.tz()).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(d.timestamp())

    async def snapshot_loop(self):
        while True:
            await asyncio.sleep(60 - time.time() % 60 + 0.5)
            ts = now() - now() % 60
            for st in list(self.stations.values()):
                try:
                    self.db.exec("INSERT OR REPLACE INTO snapshots(station_id, ts, listeners, live) VALUES(?,?,?,?)",
                                 (st.id, ts, st.listener_count(), 1 if st.mode == "live" else 0))
                except Exception as e:
                    log(f"snapshot: {e}", "error")

    async def housekeeping_loop(self):
        n = 0
        while True:
            await asyncio.sleep(5)
            n += 1
            for st in list(self.stations.values()):
                st.tick_peak()
            if n % 720 == 0:      # o dată pe oră
                self.db.exec("DELETE FROM sessions WHERE last < ?", (now() - SESSION_TTL,))
                self.geo.update_async()
                keep = int(self.db.get_setting("retention_days", "730") or 730)
                lim = now() - keep * 86400
                self.db.exec("DELETE FROM snapshots WHERE ts < ?", (lim,))
                self.db.exec("DELETE FROM listeners WHERE started < ?", (lim,))
                self.db.exec("DELETE FROM songs WHERE ts < ?", (lim,))
                self.db.exec("DELETE FROM events WHERE ts < ?", (lim,))

    # ------------------------------------------------------------------ alerte
    def alert(self, text):
        token = self.db.get_setting("telegram_token", "")
        chat = self.db.get_setting("telegram_chat", "")
        if not token or not chat:
            return

        def go():
            try:
                data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
                urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=15).read()
            except Exception as e:
                log(f"telegram: {e}", "warn")
        threading.Thread(target=go, daemon=True).start()

    # ------------------------------------------------------------------ utilitare
    def station_by_slug(self, slug):
        for st in self.stations.values():
            if st.slug == slug:
                return st
        return None

    def find_hub(self, path):
        for st in self.stations.values():
            h = st.hubs.get(path)
            if h:
                return st, h
        return None, None

    def station_by_source_pass(self, pw):
        pw = (pw or "").strip()
        if ":#" in pw:
            pw = pw.split(":#")[0]
        for st in self.stations.values():
            if pw and secrets.compare_digest(pw, st.cfg["source_pass"]):
                return st
        return None

    def station_for_source_path(self, path):
        p = path.rstrip("/")
        for st in self.stations.values():
            base = "/" + st.slug
            if p == base or p in st.hubs or p == base + "-live" or p == base + "/live":
                return st
        if len(self.stations) == 1 and p in ("", "/", "/live", "/stream"):
            return next(iter(self.stations.values()))
        return None

    def is_banned(self, ip):
        r = self.db.one("SELECT until FROM bans WHERE ip=?", (ip,))
        return bool(r) and (not r["until"] or r["until"] > now())

    # ---- protecție surse: blocare automată după încercări eșuate ----
    SRC_MAX_FAILS = 5          # încercări greșite
    SRC_WINDOW = 600           # în 10 minute
    SRC_BLOCK = 86400          # -> blocat 24 h

    def source_fail(self, ip, what):
        lst = [t for t in self.src_fails.get(ip, []) if time.time() - t < self.SRC_WINDOW]
        lst.append(time.time())
        self.src_fails[ip] = lst
        if len(lst) >= self.SRC_MAX_FAILS:
            self.src_fails.pop(ip, None)
            if ip in ("127.0.0.1", "::1"):
                return
            self.db.exec("INSERT OR REPLACE INTO bans(ip, reason, created, until) VALUES(?,?,?,?)",
                         (ip, f"auto: {self.SRC_MAX_FAILS} încercări greșite ({what})", now(), now() + self.SRC_BLOCK))
            self.db.event(None, "auto_ban", f"{ip} blocat 24h – {what}")
            log(f"IP {ip} blocat automat 24h după {self.SRC_MAX_FAILS} încercări greșite ({what})", "warn")
        elif len(lst) == 1:
            log(f"{what} respinsă de la {ip}", "warn")

    @staticmethod
    def ip_allowed(ip, allow):
        allow = (allow or "").replace(";", ",").replace(" ", ",")
        nets = [a.strip() for a in allow.split(",") if a.strip()]
        if not nets:
            return True
        import ipaddress
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        for n in nets:
            try:
                if a in ipaddress.ip_network(n, strict=False):
                    return True
            except ValueError:
                continue
        return False

    # ------------------------------------------------------------------ dispatch
    async def dispatch(self, req):
        m, p = req.method, req.path
        if m == "SOURCE" or (m == "PUT" and not p.startswith("/api/")):
            return await self.handle_icecast_source(req)
        if m == "OPTIONS":
            return await send(req.writer, 204, b"", headers={
                "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "*"})
        if p in ("/admin/metadata", "/admin/metadata.xsl", "/admin.cgi"):
            return await self.handle_metadata(req)
        if p.startswith("/api/"):
            return await self.api(req)
        if m not in ("GET", "HEAD"):
            raise HTTPError(405)
        st, hub = self.find_hub(p)
        if hub:
            return await self.serve_listener(req, st, hub)
        if p.startswith("/static/"):
            f = os.path.normpath(os.path.join(self.web_dir, p[len("/static/"):]))
            if not f.startswith(os.path.normpath(self.web_dir)):
                raise HTTPError(403)
            return await send_file(req.writer, f)
        if p == "/" or p == "/login":
            u = self.current_user(req)
            if u and p == "/":
                return await send(req.writer, 302, "", headers={"Location": "/admin" if u["role"] == "admin"
                                                                 else "/manager"})
            return await send_file(req.writer, os.path.join(self.web_dir, "login.html"))
        if p == "/admin":
            u = self.current_user(req)
            if not u or u["role"] != "admin":
                return await send(req.writer, 302, "", headers={"Location": "/login"})
            return await send_file(req.writer, os.path.join(self.web_dir, "admin.html"))
        if p == "/manager":
            if not self.current_user(req):
                return await send(req.writer, 302, "", headers={"Location": "/login?next=/manager"})
            return await send_file(req.writer, os.path.join(self.web_dir, "manager.html"))
        if re.fullmatch(r"/report/\d+", p):
            if not self.current_user(req):
                return await send(req.writer, 302, "", headers={"Location": "/login"})
            return await send_file(req.writer, os.path.join(self.web_dir, "report.html"))
        mm = re.fullmatch(r"/(s|embed)/([a-z0-9\-]+)/?", p)
        if mm:
            if not self.station_by_slug(mm.group(2)):
                raise HTTPError(404, "Post inexistent")
            return await send_file(req.writer, os.path.join(self.web_dir, "player.html"),
                                   {"Access-Control-Allow-Origin": "*"})
        mm = re.fullmatch(r"/listen/([a-z0-9\-]+)(\.aac)?\.(m3u|pls)", p)
        if mm:
            return await self.playlist(req, mm.group(1), mm.group(2), mm.group(3))
        mm = re.fullmatch(r"/logo/([a-z0-9\-]+)", p)
        if mm:
            f = os.path.join(self.data_dir, "stations", mm.group(1), "logo")
            if not os.path.isfile(f):
                return await send_file(req.writer, os.path.join(self.web_dir, "logo.svg"))
            with open(f, "rb") as fh:
                data = fh.read()
            ct = "image/png"
            if data[:3] == b"\xff\xd8\xff":
                ct = "image/jpeg"
            elif data.lstrip()[:4] in (b"<svg", b"<?xm"):
                ct = "image/svg+xml"
            elif data[:4] == b"RIFF":
                ct = "image/webp"
            return await send(req.writer, 200, data, ct, {"Access-Control-Allow-Origin": "*",
                                                          "Cache-Control": "max-age=300"})
        if p == "/status-json.xsl":
            return await send_json(req.writer, self.icecast_status(req), headers={"Access-Control-Allow-Origin": "*"})
        if p in ("/7.html", "/stats") or re.fullmatch(r"/[a-z0-9\-]+/(7\.html|stats)", p):
            return await self.shoutcast_compat(req)
        if p == "/favicon.ico":
            return await send_file(req.writer, os.path.join(self.web_dir, "logo.svg"))
        raise HTTPError(404, "Nu există")

    # ------------------------------------------------------------------ ascultători
    async def serve_listener(self, req, st, hub):
        w = req.writer
        if self.is_banned(req.ip):
            raise HTTPError(403, "Acces interzis")
        maxl = int(st.cfg.get("max_listeners") or 0)
        if maxl and st.listener_count() >= maxl:
            raise HTTPError(503, "Număr maxim de ascultători atins")
        cfg = st.cfg
        h = [f"HTTP/1.0 200 OK", f"Content-Type: {hub.ctype}", "Server: NtS-Radio",
             "Cache-Control: no-cache, no-store", "Pragma: no-cache", "Access-Control-Allow-Origin: *",
             "Access-Control-Expose-Headers: icy-br, icy-name, icy-genre, icy-metaint",
             f"icy-br: {hub.bitrate}", f"icy-name: {cfg['name']}", f"icy-genre: {cfg.get('genre') or ''}",
             f"icy-description: {cfg.get('description') or ''}", f"icy-url: {cfg.get('website') or ''}",
             "icy-pub: 1", "Connection: close"]
        if req.headers.get("icy-metadata") == "1":
            h.append("icy-metaint: 16000")
        head = ("\r\n".join(h) + "\r\n\r\n").encode("utf-8", "replace")
        req.taken = True
        w.write(head)
        if req.method == "HEAD":
            await w.drain()
            return
        country = self.geo.country(req.ip)
        l = Listener(req, st, hub, country)
        if req.query.get("monitor") == "1" and self.current_user(req):
            l.internal = True
        elif req.method == "GET":
            try:
                l.intro = await st.get_intro(hub)
            except Exception:
                l.intro = None
        hub.listeners[l.id] = l
        try:
            if not l.internal:
                l.session_id = self.db.exec(
                "INSERT INTO listeners(station_id, mount, ip, ua, country, started) VALUES(?,?,?,?,?,?)",
                    (st.id, hub.mount, l.ip, l.ua[:300], country, int(l.started)))
        except Exception as e:
            log(f"db listeners: {e}", "error")
        try:
            await l.run()
        except Exception:
            pass
        finally:
            l.close()
            hub.listeners.pop(l.id, None)
            if l.session_id:
                end = int(time.time())
                try:
                    self.db.exec("UPDATE listeners SET ended=?, duration=? WHERE id=?",
                                 (end, end - int(l.started), l.session_id))
                except Exception:
                    pass

    async def playlist(self, req, slug, aac, kind):
        st = self.station_by_slug(slug)
        if not st:
            raise HTTPError(404)
        base = self.public_base(req)
        url = f"{base}/{slug}{aac or ''}"
        if aac and ("/" + slug + aac) not in st.hubs:
            raise HTTPError(404)
        name = st.cfg["name"]
        if kind == "m3u":
            body, ct = f"#EXTM3U\n#EXTINF:-1,{name}\n{url}\n", "audio/x-mpegurl"
        else:
            body, ct = f"[playlist]\nNumberOfEntries=1\nFile1={url}\nTitle1={name}\nLength1=-1\nVersion=2\n", \
                "audio/x-scpls"
        await send(req.writer, 200, body, ct, {"Content-Disposition": f'attachment; filename="{slug}.{kind}"'})

    def public_base(self, req):
        pu = (self.db.get_setting("public_url", "") or "").rstrip("/")
        if pu:
            return pu
        host = req.headers.get("host") or f"localhost:{self.port}"
        proto = req.headers.get("x-forwarded-proto", "http")
        return f"{proto}://{host}"

    # ------------------------------------------------------------------ surse (studio)
    async def handle_icecast_source(self, req):
        if self.is_banned(req.ip):
            raise HTTPError(403, "Acces interzis")
        st = self.station_for_source_path(req.path)
        auth = req.headers.get("authorization", "")
        pw = ""
        if auth.lower().startswith("basic "):
            try:
                pw = base64.b64decode(auth[6:]).decode("utf-8", "replace").split(":", 1)[1]
            except Exception:
                pw = ""
        by_pw = self.station_by_source_pass(pw)
        if st is None and by_pw is not None:
            st = by_pw
        if st is None or by_pw is not st:
            self.source_fail(req.ip, f"sursă Icecast pe {req.path[:40]} (parolă greșită)")
            raise HTTPError(401, "Autentificare eșuată")
        if not self.ip_allowed(req.ip, st.cfg.get("source_ips")):
            self.source_fail(req.ip, f"sursă Icecast pentru {st.slug} (IP nepermis)")
            raise HTTPError(403, "IP nepermis pentru sursă")
        live = LiveInput(st, req.ip, req.headers.get("user-agent"), req.headers.get("content-type"),
                         "Icecast", req.headers)
        ok, msg = await st.attach_live(live)
        if not ok:
            raise HTTPError(403, msg)
        req.taken = True
        w = req.writer
        if req.headers.get("expect", "").lower() == "100-continue":
            w.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        w.write(b"HTTP/1.0 200 OK\r\nServer: NtS-Radio\r\n\r\n")
        await w.drain()
        reader = ChunkedReader(req.reader) if "chunked" in req.headers.get("transfer-encoding", "").lower() \
            else req.reader
        await self._pump_source(st, live, reader)

    async def handle_sc1(self, reader, writer):
        """SHOUTcast v1: encoderul se conectează pe port+1, trimite parola, apoi headere icy-* și audio"""
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        try:
            if self.is_banned(ip):
                return
            line = await asyncio.wait_for(reader.readline(), 10)
            pw = line.decode("utf-8", "replace").strip()
            if re.match(r"^(GET|POST|HEAD|OPTIONS|PUT|CONNECT|PRI) ", pw) or not pw.isprintable() or len(pw) > 200:
                self.source_fail(ip, "scanare pe portul SHOUTcast")
                return
            st = self.station_by_source_pass(pw)
            if not st:
                self.source_fail(ip, "sursă SHOUTcast (parolă greșită)")
                writer.write(b"invalid password\r\n")
                await writer.drain()
                return
            if not self.ip_allowed(ip, st.cfg.get("source_ips")):
                self.source_fail(ip, f"sursă SHOUTcast pentru {st.slug} (IP nepermis)")
                writer.write(b"invalid password\r\n")
                await writer.drain()
                return
            if st.live and time.time() - st.live.last_data < 3:
                writer.write(b"Stream In Use\r\n")
                await writer.drain()
                return
            writer.write(b"OK2\r\nicy-caps:11\r\n\r\n")
            await writer.drain()
            headers = {}
            for _ in range(50):
                hl = await asyncio.wait_for(reader.readline(), 10)
                s = hl.decode("utf-8", "replace").strip()
                if not s:
                    break
                if ":" in s:
                    k, v = s.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            live = LiveInput(st, ip, headers.get("user-agent") or headers.get("icy-name") or "SHOUTcast encoder",
                             headers.get("content-type", "audio/mpeg"), "SHOUTcast v1", headers)
            ok, msg = await st.attach_live(live)
            if not ok:
                return
            await self._pump_source(st, live, reader)
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _pump_source(self, st, live, reader):
        try:
            while not live.kicked and not live.closed:
                data = await asyncio.wait_for(reader.read(16384), 15)
                if not data:
                    break
                await live.feed(data)
        except Exception:
            pass
        finally:
            await st.detach_live(live)

    async def handle_metadata(self, req):
        if self.is_banned(req.ip):
            raise HTTPError(403, "Acces interzis")
        q = req.query
        pw = q.get("pass", "")
        auth = req.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                pw = base64.b64decode(auth[6:]).decode("utf-8", "replace").split(":", 1)[1]
            except Exception:
                pass
        st = self.station_by_source_pass(pw)
        mount = q.get("mount")
        if st and mount:
            st2 = self.station_for_source_path(mount)
            if st2 is not None and st2 is not st:
                st = None
        if not st:
            self.source_fail(req.ip, "actualizare metadate (parolă greșită)")
            raise HTTPError(401, "Autentificare eșuată")
        raw = urllib.parse.parse_qs(req.raw_query, keep_blank_values=True,
                                    encoding="latin-1").get("song", [""])[-1]
        song = decode_text(raw.encode("latin-1", "replace")).strip()
        if song:
            st.set_live_title(song)
        if req.path.startswith("/admin/metadata"):
            await send(req.writer, 200, "<?xml version=\"1.0\"?>\n<iceresponse><message>Metadata update successful"
                                        "</message><return>1</return></iceresponse>\n", "text/xml")
        else:
            await send(req.writer, 200, "<html><body>OK</body></html>", "text/html")

    # ------------------------------------------------------------------ compatibilitate
    def icecast_status(self, req):
        base = self.public_base(req)
        srcs = []
        for st in self.stations.values():
            for h in st.hubs.values():
                srcs.append({"listenurl": base + h.mount, "server_name": st.cfg["name"],
                             "server_description": st.cfg.get("description") or "",
                             "genre": st.cfg.get("genre") or "", "title": st.title, "listeners": h.count(),
                             "listener_peak": st.peak_today[1], "bitrate": h.bitrate,
                             "server_type": h.ctype, "stream_start_iso8601": ""})
        return {"icestats": {"admin": "", "host": req.headers.get("host", ""), "server_id": f"NtS-Radio {VERSION}",
                             "source": srcs[0] if len(srcs) == 1 else srcs}}

    async def shoutcast_compat(self, req):
        parts = req.path.strip("/").split("/")
        st = None
        ordered = sorted(self.stations.values(), key=lambda x: x.id)
        if len(parts) == 2:
            st = self.station_by_slug(parts[0])
        elif "sid" in req.query:
            try:
                st = ordered[int(req.query["sid"]) - 1]
            except Exception:
                st = None
        if st is None and ordered:
            st = ordered[0]
        if not st:
            raise HTTPError(404)
        n = st.listener_count()
        br = next(iter(st.hubs.values())).bitrate if st.hubs else 128
        src = 1 if st.mode != "silence" else 0
        if parts[-1] == "7.html":
            body = f"<html><body>{n},{src},{st.peak_today[1]},{st.cfg.get('max_listeners') or 0},{n},{br}," \
                   f"{st.title.replace(',', ' ')}</body></html>"
            return await send(req.writer, 200, body, "text/html")
        if req.query.get("json") == "1":
            return await send_json(req.writer, {
                "currentlisteners": n, "peaklisteners": st.peak_today[1], "maxlisteners": st.cfg.get("max_listeners"),
                "uniquelisteners": len({l.ip for h in st.hubs.values() for l in h.listeners.values()}),
                "averagetime": 0, "servergenre": st.cfg.get("genre"), "serverurl": st.cfg.get("website"),
                "servertitle": st.cfg["name"], "songtitle": st.title, "streamstatus": src, "bitrate": br,
                "content": "audio/mpeg", "version": f"NtS-Radio {VERSION}"}, headers={"Access-Control-Allow-Origin": "*"})
        from xml.sax.saxutils import escape
        body = (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><SHOUTCASTSERVER><CURRENTLISTENERS>{n}</CURRENTLISTENERS>"
                f"<PEAKLISTENERS>{st.peak_today[1]}</PEAKLISTENERS><MAXLISTENERS>{st.cfg.get('max_listeners')}"
                f"</MAXLISTENERS><SERVERTITLE>{escape(st.cfg['name'])}</SERVERTITLE><SONGTITLE>{escape(st.title)}"
                f"</SONGTITLE><STREAMSTATUS>{src}</STREAMSTATUS><BITRATE>{br}</BITRATE></SHOUTCASTSERVER>")
        return await send(req.writer, 200, body, "text/xml")

    # ------------------------------------------------------------------ autentificare
    def current_user(self, req):
        if req.user is not None:
            return req.user or None
        tok = req.cookies.get(SESSION_COOKIE)
        u = None
        if tok:
            s = self.db.one("SELECT user_id, last FROM sessions WHERE token=?", (tok,))
            if s and now() - s["last"] < SESSION_TTL:
                u = self.db.one("SELECT id, username, role, station_id FROM users WHERE id=?", (s["user_id"],))
                if u and now() - s["last"] > 300:
                    self.db.exec("UPDATE sessions SET last=? WHERE token=?", (now(), tok))
        req.user = u or False
        return u

    def need_user(self, req):
        u = self.current_user(req)
        if not u:
            raise HTTPError(401, "Neautentificat")
        return u

    def need_admin(self, req):
        u = self.need_user(req)
        if u["role"] != "admin":
            raise HTTPError(403, "Doar administratorul")
        return u

    def need_station(self, req, sid):
        u = self.need_user(req)
        if u["role"] != "admin" and u["station_id"] != sid:
            raise HTTPError(403, "Fără acces la acest post")
        cfg = self.db.station(sid)
        if not cfg:
            raise HTTPError(404, "Post inexistent")
        return u, cfg

    # ------------------------------------------------------------------ API
    async def api(self, req):
        m, p, w = req.method, req.path, req.writer
        seg = p.strip("/").split("/")[1:]       # fără "api"

        def num(i):
            try:
                return int(seg[i])
            except Exception:
                raise HTTPError(404)

        # ---------- public ----------
        if seg[:1] == ["public"] and len(seg) == 2:
            st = self.station_by_slug(seg[1])
            if not st:
                raise HTTPError(404, "Post inexistent")
            return await send_json(w, self.public_info(req, st), headers={"Access-Control-Allow-Origin": "*"})

        if seg == ["login"] and m == "POST":
            return await self.api_login(req)
        if seg == ["logout"]:
            tok = req.cookies.get(SESSION_COOKIE)
            if tok:
                self.db.exec("DELETE FROM sessions WHERE token=?", (tok,))
            return await send_json(w, {"ok": True}, headers={
                "Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
        if seg == ["me"]:
            u = self.need_user(req)
            return await send_json(w, {**u, "version": VERSION})
        if seg == ["me", "password"] and m == "PUT":
            u = self.need_user(req)
            d = await req.json()
            row = self.db.one("SELECT pass FROM users WHERE id=?", (u["id"],))
            if not check_password(d.get("old", ""), row["pass"]):
                raise HTTPError(400, "Parola actuală e greșită")
            if len(d.get("new", "")) < 6:
                raise HTTPError(400, "Parola nouă trebuie să aibă minim 6 caractere")
            self.db.exec("UPDATE users SET pass=? WHERE id=?", (hash_password(d["new"]), u["id"]))
            return await send_json(w, {"ok": True})

        # ---------- posturi accesibile ----------
        if seg == ["stations"]:
            u = self.need_user(req)
            rows = self.db.stations()
            if u["role"] != "admin":
                rows = [r for r in rows if r["id"] == u["station_id"]]
            return await send_json(w, [{"id": r["id"], "name": r["name"], "slug": r["slug"]} for r in rows])

        if seg[:1] == ["station"] and len(seg) >= 3:
            sid = num(1)
            u, cfg = self.need_station(req, sid)
            return await self.api_station(req, sid, cfg, seg[2:])

        if seg[:1] == ["admin"]:
            self.need_admin(req)
            return await self.api_admin(req, seg[1:])
        raise HTTPError(404, "Endpoint inexistent")

    async def api_login(self, req):
        ip = req.ip
        fails = [t for t in self.login_fail.get(ip, []) if time.time() - t < 600]
        if len(fails) >= 10:
            raise HTTPError(429, "Prea multe încercări. Reîncearcă peste 10 minute.")
        d = await req.json()
        u = self.db.one("SELECT * FROM users WHERE username=?", (d.get("username", "").strip(),))
        if not u or not check_password(d.get("password", ""), u["pass"]):
            fails.append(time.time())
            self.login_fail[ip] = fails
            await asyncio.sleep(1)
            raise HTTPError(401, "Utilizator sau parolă greșită")
        self.login_fail.pop(ip, None)
        tok = secrets.token_urlsafe(32)
        self.db.exec("INSERT INTO sessions(token, user_id, created, last) VALUES(?,?,?,?)", (tok, u["id"], now(), now()))
        secure = "; Secure" if req.headers.get("x-forwarded-proto") == "https" else ""
        return await send_json(req.writer, {"ok": True, "role": u["role"]}, headers={
            "Set-Cookie": f"{SESSION_COOKIE}={tok}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax{secure}"})

    def public_info(self, req, st):
        base = self.public_base(req)
        rows = self.db.all("SELECT ts, title, source FROM songs WHERE station_id=? AND source!='off' "
                           "ORDER BY ts DESC LIMIT 11", (st.id,))
        return {"name": st.cfg["name"], "slug": st.slug, "genre": st.cfg.get("genre"),
                "description": st.cfg.get("description"), "website": st.cfg.get("website"),
                "title": st.title, "live": st.mode == "live", "on_air": st.mode != "silence",
                "listeners": st.listener_count(), "logo": f"{base}/logo/{st.slug}",
                "streams": [{"url": base + h.mount, "codec": h.codec, "bitrate": h.bitrate} for h in st.hubs.values()],
                "history": rows[1:] if rows and rows[0]["title"] == st.title else rows[:10]}

    def period_args(self, req):
        q = req.query
        try:
            frm = int(q.get("from") or 0)
            to = int(q.get("to") or 0)
        except ValueError:
            raise HTTPError(400, "Perioadă invalidă")
        if not to:
            to = now()
        if not frm:
            frm = self.day_start()
        if to <= frm:
            raise HTTPError(400, "Perioadă invalidă")
        return frm, min(to, frm + 400 * 86400)

    async def run_stats(self, fn, *args):
        def go():
            c = self.db.reader()
            try:
                return fn(c, *args)
            finally:
                c.close()
        return await asyncio.to_thread(go)

    async def api_station(self, req, sid, cfg, seg):
        m, w = req.method, req.writer
        st = self.stations.get(sid)
        if seg == ["live"]:
            base = self.public_base(req)
            res = {"name": cfg["name"], "slug": cfg["slug"], "running": bool(st), "mode": st.mode if st else "off",
                   "title": st.title if st else "", "listeners": st.listener_count() if st else 0,
                   "peak_today": st.peak_today[1] if st else 0, "tz": self.tzname()}
            if st:
                lis = [l.info() for h in st.hubs.values() for l in h.listeners.values() if not l.internal]
                cc, dev = {}, {}
                for l in lis:
                    cc[l["country"] or "?"] = cc.get(l["country"] or "?", 0) + 1
                    dev[l["device"]] = dev.get(l["device"], 0) + 1
                res["countries"] = sorted(cc.items(), key=lambda x: -x[1])
                res["devices"] = sorted(dev.items(), key=lambda x: -x[1])
                res["avg_time"] = int(sum(l["duration"] for l in lis) / len(lis)) if lis else 0
                res["live_since"] = int(st.live.connected_at) if st.live else None
                res["studio_connected"] = bool(st.live)
                res["mounts"] = [{"url": base + h.mount, "codec": h.codec, "bitrate": h.bitrate,
                                  "listeners": h.count(), "path": h.mount} for h in st.hubs.values()]
            t0 = now() - 3 * 3600
            res["recent"] = self.db.all("SELECT ts, listeners FROM snapshots WHERE station_id=? AND ts>=? ORDER BY ts",
                                        (sid, t0))
            res["today"] = self.db.one(
                "SELECT COUNT(DISTINCT ip) AS unique_ips, COALESCE(SUM(MIN(COALESCE(ended,?),?) - MAX(started,?)),0) "
                "AS secs FROM listeners WHERE station_id=? AND COALESCE(ended,?) > ? ",
                (now(), now(), self.day_start(), sid, now(), self.day_start()))
            res["songs"] = self.db.all("SELECT ts, title, source, listeners FROM songs WHERE station_id=? "
                                       "AND source!='off' ORDER BY ts DESC LIMIT 15", (sid,))
            res["public"] = {"player": f"{base}/s/{cfg['slug']}", "embed": f"{base}/embed/{cfg['slug']}",
                             "m3u": f"{base}/listen/{cfg['slug']}.m3u", "pls": f"{base}/listen/{cfg['slug']}.pls"}
            return await send_json(w, res)
        if seg == ["stats"]:
            frm, to = self.period_args(req)
            shows = self.db.all("SELECT * FROM shows WHERE station_id=?", (sid,))
            res = await self.run_stats(stats.period, sid, frm, to, self.tzname(), shows)
            res["station"] = cfg["name"]
            res["tz"] = self.tzname()
            return await send_json(w, res)
        if seg == ["songs"]:
            frm, to = self.period_args(req)
            res = await self.run_stats(stats.history, sid, frm, to, min(int(req.query.get("limit", 200)), 1000),
                                       int(req.query.get("offset", 0)), req.query.get("q", ""))
            return await send_json(w, res)
        if seg == ["top"]:
            frm, to = self.period_args(req)
            return await send_json(w, await self.run_stats(stats.top_songs, sid, frm, to, 50))
        if seg[:1] == ["shows"]:
            if m == "GET" and len(seg) == 1:
                return await send_json(w, self.db.all("SELECT * FROM shows WHERE station_id=? ORDER BY start", (sid,)))
            if m in ("POST", "PUT"):
                d = await req.json()
                name = (d.get("name") or "").strip()
                days = ",".join(str(int(x)) for x in d.get("days", []) if 1 <= int(x) <= 7)
                start, end = d.get("start", ""), d.get("end", "")
                if not name or not days or not re.fullmatch(r"\d{2}:\d{2}", start) or \
                        not re.fullmatch(r"\d{2}:\d{2}", end):
                    raise HTTPError(400, "Completează numele, zilele și orele (HH:MM)")
                if m == "POST":
                    self.db.exec("INSERT INTO shows(station_id, name, host, days, start, end) VALUES(?,?,?,?,?,?)",
                                 (sid, name, d.get("host", ""), days, start, end))
                else:
                    self.db.exec("UPDATE shows SET name=?, host=?, days=?, start=?, end=? WHERE id=? AND station_id=?",
                                 (name, d.get("host", ""), days, start, end, int(seg[1]), sid))
                return await send_json(w, {"ok": True})
            if m == "DELETE" and len(seg) == 2:
                self.db.exec("DELETE FROM shows WHERE id=? AND station_id=?", (int(seg[1]), sid))
                return await send_json(w, {"ok": True})
        if seg[:1] == ["export"] and len(seg) == 2:
            return await self.export_csv(req, sid, cfg, seg[1])
        raise HTTPError(404)

    async def export_csv(self, req, sid, cfg, name):
        frm, to = self.period_args(req)
        out = io.StringIO()
        wr = csv.writer(out, delimiter=";")
        tz = self.tz()
        import datetime as dt

        def fmt(ts):
            return dt.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S")
        if name == "zilnic.csv":
            shows = self.db.all("SELECT * FROM shows WHERE station_id=?", (sid,))
            r = await self.run_stats(stats.period, sid, frm, to, self.tzname(), shows)
            wr.writerow(["Data", "Ore ascultate (TLH)", "Ascultatori unici", "Medie ascultatori", "Varf ascultatori"])
            for d in r["daily"]:
                wr.writerow([d["day"], str(d["tlh"]).replace(".", ","), d["unique"], str(d["avg"]).replace(".", ","),
                             d["peak"]])
        elif name == "piese.csv":
            r = await self.run_stats(stats.history, sid, frm, to, 100000, 0, "")
            wr.writerow(["Data/ora", "Piesa", "Sursa", "Ascultatori"])
            for s in r["rows"]:
                wr.writerow([fmt(s["ts"]), s["title"], s["source"], s["listeners"]])
        elif name == "emisiuni.csv":
            shows = self.db.all("SELECT * FROM shows WHERE station_id=?", (sid,))
            r = await self.run_stats(stats.period, sid, frm, to, self.tzname(), shows)
            wr.writerow(["Emisiune", "Prezentator", "Zile", "Interval", "Difuzari", "Medie ascultatori", "Varf", "TLH"])
            for s in r["shows"]:
                wr.writerow([s["name"], s["host"], s["days"], f"{s['start']}-{s['end']}", s["occurrences"],
                             str(s["avg"]).replace(".", ","), s["peak"], str(s["tlh"]).replace(".", ",")])
        elif name == "tari.csv":
            r = await self.run_stats(stats.period, sid, frm, to, self.tzname(), [])
            wr.writerow(["Tara", "Ascultatori unici", "TLH", "Procent"])
            for c in r["countries"]:
                wr.writerow([c["code"], c["unique"], str(c["tlh"]).replace(".", ","), str(c["pct"]).replace(".", ",")])
        else:
            raise HTTPError(404)
        data = "﻿" + out.getvalue()      # BOM -> Excel deschide corect diacriticele
        fname = f"{cfg['slug']}-{name[:-4]}-{dt.datetime.fromtimestamp(frm, tz):%Y%m%d}-" \
                f"{dt.datetime.fromtimestamp(to, tz):%Y%m%d}.csv"
        await send(req.writer, 200, data, "text/csv; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="{fname}"'})

    # ------------------------------------------------------------------ API admin
    def station_payload(self, d, existing=None):
        e = existing or {}
        name = (d.get("name") if "name" in d else e.get("name") or "").strip()
        if not name:
            raise HTTPError(400, "Numele postului e obligatoriu")
        slug = slugify(d.get("slug") or e.get("slug") or name)
        if slug in ("api", "static", "admin", "manager", "login", "s", "embed", "listen", "logo", "report",
                    "status-json.xsl", "stats"):
            raise HTTPError(400, "Slug rezervat, alege altul")
        other = self.db.one("SELECT id FROM stations WHERE slug=?", (slug,))
        if other and other["id"] != e.get("id"):
            raise HTTPError(409, "Există deja un post cu acest slug")
        pw = (d.get("source_pass") or e.get("source_pass") or gen_password(10)).strip()
        if len(pw) < 6 or ":" in pw:
            raise HTTPError(400, "Parola sursei: minim 6 caractere, fără ':'")
        o = self.db.one("SELECT id FROM stations WHERE source_pass=?", (pw,))
        if o and o["id"] != e.get("id"):
            raise HTTPError(409, "Parola sursei e folosită de alt post (trebuie să fie unică)")
        outs = d.get("outputs", e.get("outputs")) or DEFAULT_OUTPUTS
        clean, seen = [], set()
        for o in outs:
            mount = (o.get("mount") or "").strip()
            if mount and not re.fullmatch(r"[a-zA-Z0-9.\-_]+", mount):
                raise HTTPError(400, f"Sufix mount invalid: {mount}")
            codec = o.get("codec", "mp3")
            if codec not in ("mp3", "aac"):
                raise HTTPError(400, "Codec: mp3 sau aac")
            br = int(o.get("bitrate") or 128)
            if br < 16 or br > 320:
                raise HTTPError(400, "Bitrate între 16 și 320 kbps")
            if mount in seen:
                raise HTTPError(400, "Mounturi duplicate")
            seen.add(mount)
            clean.append({"mount": mount, "codec": codec, "bitrate": br})
        if not clean:
            raise HTTPError(400, "Cel puțin o ieșire")

        # ---- jingle la conectare ----
        intro = str(d.get("intro", e.get("intro", "")) or "").strip()
        if intro and intro != "*" and (".." in intro or intro.startswith("/")):
            raise HTTPError(400, "Fișier jingle invalid")
        # ---- IP-uri permise pentru sursă ----
        import ipaddress
        src_ips = ",".join(x.strip() for x in str(d.get("source_ips", e.get("source_ips", "")) or "")
                           .replace(";", ",").replace(" ", ",").split(",") if x.strip())
        for n in filter(None, src_ips.split(",")):
            try:
                ipaddress.ip_network(n, strict=False)
            except ValueError:
                raise HTTPError(400, f"IP / rețea invalidă: {n}")
        # ---- relay de intrare ----
        rin = d.get("relay", e.get("relay")) or {}
        rurl = (rin.get("url") or "").strip()
        if rurl and not re.match(r"^https?://", rurl, re.I):
            raise HTTPError(400, "Adresa relay trebuie să înceapă cu http:// sau https://")
        if rin.get("enabled") and not rurl:
            raise HTTPError(400, "Completează adresa stream-ului pentru relay")
        sched = []
        for it in rin.get("schedule") or []:
            days = sorted({int(x) for x in it.get("days") or [] if 1 <= int(x) <= 7})
            st_, en_ = it.get("start", ""), it.get("end", "")
            if not days or not re.fullmatch(r"\d{2}:\d{2}", st_) or not re.fullmatch(r"\d{2}:\d{2}", en_):
                raise HTTPError(400, "Program relay: alege zilele și orele (HH:MM)")
            sched.append({"days": days, "start": st_, "end": en_})
        relay = {"enabled": 1 if rin.get("enabled") else 0, "url": rurl, "schedule": sched}
        # ---- retransmisii (relay de ieșire) ----
        pushes = []
        suffixes = {o["mount"] for o in clean}
        for pc in d.get("pushes", e.get("pushes")) or []:
            t = pc.get("type")
            if t not in ("icecast", "shoutcast", "rtmp"):
                raise HTTPError(400, "Tip retransmisie invalid")
            item = {"name": (pc.get("name") or "").strip()[:60], "type": t, "enabled": 1 if pc.get("enabled", 1) else 0,
                    "output": pc.get("output") if pc.get("output") in suffixes else clean[0]["mount"]}
            if t == "rtmp":
                url = (pc.get("url") or "").strip()
                if not re.match(r"^rtmps?://", url, re.I):
                    raise HTTPError(400, "Adresa RTMP trebuie să înceapă cu rtmp:// sau rtmps://")
                item.update({"url": url, "key": (pc.get("key") or "").strip(), "video": 1 if pc.get("video", 1) else 0})
            else:
                host = (pc.get("host") or "").strip()
                if not host:
                    raise HTTPError(400, "Retransmisie: completează serverul destinație")
                try:
                    port = int(pc.get("port") or 8000)
                except ValueError:
                    raise HTTPError(400, "Port invalid")
                item.update({"host": host, "port": port, "password": pc.get("password") or "",
                             "tls": 1 if pc.get("tls") else 0})
                if t == "icecast":
                    item.update({"mount": "/" + (pc.get("mount") or "stream").strip().lstrip("/"),
                                 "user": (pc.get("user") or "source").strip()})
            pushes.append(item)

        def g(k, default, typ=int):
            v = d.get(k, e.get(k, default))
            try:
                return typ(v)
            except Exception:
                return default
        return {"name": name, "slug": slug, "genre": d.get("genre", e.get("genre", "")) or "",
                "description": d.get("description", e.get("description", "")) or "",
                "website": d.get("website", e.get("website", "")) or "", "source_pass": pw,
                "max_listeners": g("max_listeners", 1000), "enabled": 1 if g("enabled", 1) else 0,
                "autodj": 1 if g("autodj", 1) else 0, "shuffle": 1 if g("shuffle", 1) else 0,
                "crossfade": max(0.0, min(10.0, g("crossfade", 3, float))),
                "jingle_every": max(0, g("jingle_every", 4)), "silence_secs": max(0, g("silence_secs", 0)),
                "outputs": clean, "relay": relay, "pushes": pushes, "source_ips": src_ips,
                "intro": intro}

    async def api_admin(self, req, seg):
        m, w = req.method, req.writer
        if seg == ["overview"]:
            return await send_json(w, {"stations": [st.status() for st in self.stations.values()],
                                       "stopped": [s for s in self.db.stations() if s["id"] not in self.stations],
                                       "server": self.server_info(), "version": VERSION})
        if seg == ["stations"]:
            if m == "GET":
                return await send_json(w, self.db.stations())
            if m == "POST":
                d = self.station_payload(await req.json())
                sid = self.db.exec(
                    "INSERT INTO stations(slug,name,genre,description,website,source_pass,max_listeners,enabled,autodj,"
                    "shuffle,crossfade,jingle_every,silence_secs,outputs,relay,pushes,source_ips,intro,created) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (d["slug"], d["name"], d["genre"], d["description"], d["website"], d["source_pass"],
                     d["max_listeners"], d["enabled"], d["autodj"], d["shuffle"], d["crossfade"], d["jingle_every"],
                     d["silence_secs"], json.dumps(d["outputs"]), json.dumps(d["relay"]), json.dumps(d["pushes"]),
                     d["source_ips"], d["intro"], now()))
                if d["enabled"]:
                    await self.start_station(self.db.station(sid))
                self.db.event(sid, "config", "post creat")
                return await send_json(w, {"ok": True, "id": sid})
        if seg[:1] == ["stations"] and len(seg) >= 2:
            sid = int(seg[1])
            cfg = self.db.station(sid)
            if not cfg:
                raise HTTPError(404, "Post inexistent")
            st = self.stations.get(sid)
            rest = seg[2:]
            if not rest and m == "PUT":
                d = self.station_payload(await req.json(), cfg)
                if d["slug"] != cfg["slug"]:
                    old = os.path.join(self.data_dir, "stations", cfg["slug"])
                    new = os.path.join(self.data_dir, "stations", d["slug"])
                    if os.path.isdir(old) and not os.path.exists(new):
                        if st:
                            await self.stop_station(sid)
                            st = None
                        os.rename(old, new)
                self.db.exec("UPDATE stations SET slug=?,name=?,genre=?,description=?,website=?,source_pass=?,"
                             "max_listeners=?,enabled=?,autodj=?,shuffle=?,crossfade=?,jingle_every=?,silence_secs=?,"
                             "outputs=?,relay=?,pushes=?,source_ips=?,intro=? WHERE id=?",
                             (d["slug"], d["name"], d["genre"], d["description"], d["website"], d["source_pass"],
                              d["max_listeners"], d["enabled"], d["autodj"], d["shuffle"], d["crossfade"],
                              d["jingle_every"], d["silence_secs"], json.dumps(d["outputs"]), json.dumps(d["relay"]),
                              json.dumps(d["pushes"]), d["source_ips"], d["intro"], sid))
                new_cfg = self.db.station(sid)
                restart = (st is not None) != bool(new_cfg["enabled"]) or (st and (
                    new_cfg["outputs"] != cfg["outputs"] or new_cfg["slug"] != cfg["slug"]))
                if restart:
                    await self.stop_station(sid)
                    if new_cfg["enabled"]:
                        await self.start_station(new_cfg)
                elif st:
                    await st.apply_cfg(new_cfg)
                    if st.live and new_cfg["source_pass"] != cfg["source_pass"]:
                        st.live.kicked = True
                        await st.live.close()
                self.db.event(sid, "config", "setări modificate" + (" (repornit)" if restart else ""))
                return await send_json(w, {"ok": True, "restarted": bool(restart)})
            if not rest and m == "DELETE":
                await self.stop_station(sid)
                self.db.exec("DELETE FROM stations WHERE id=?", (sid,))
                self.db.exec("UPDATE users SET station_id=NULL WHERE station_id=?", (sid,))
                return await send_json(w, {"ok": True})
            if rest == ["restart"] and m == "POST":
                await self.stop_station(sid)
                if cfg["enabled"]:
                    await self.start_station(cfg)
                return await send_json(w, {"ok": True})
            if rest == ["skip"] and m == "POST":
                if st:
                    await st.autodj.skip()
                return await send_json(w, {"ok": True})
            if rest == ["kick-source"] and m == "POST":
                if st and st.live:
                    st.live.kicked = True
                    await st.live.close()
                    self.db.event(sid, "source_kick", "sursa deconectată de admin")
                return await send_json(w, {"ok": True})
            if rest == ["logo"] and m == "POST":
                body = await req.body(limit=3 * 1024 * 1024)
                d = os.path.join(self.data_dir, "stations", cfg["slug"])
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "logo"), "wb") as f:
                    f.write(body)
                return await send_json(w, {"ok": True})
            if rest == ["files"]:
                return await self.api_files(req, cfg, st)
        if seg == ["jingles-all"]:
            res = []
            for cfg in self.db.stations():
                jd = os.path.join(self.data_dir, "stations", cfg["slug"], "jingles")
                for root, _, files in os.walk(jd):
                    for f in sorted(files):
                        if os.path.splitext(f)[1].lower() in {".mp3", ".aac", ".m4a", ".ogg", ".opus", ".flac", ".wav"}:
                            rel = os.path.relpath(os.path.join(root, f), jd)
                            res.append({"station_id": cfg["id"], "station": cfg["name"], "slug": cfg["slug"],
                                        "name": rel, "value": f"{cfg['slug']}/{rel}"})
            return await send_json(w, res)
        if seg == ["listeners"]:
            res = []
            for st in self.stations.values():
                for h in st.hubs.values():
                    res += [l.info() for l in h.listeners.values()]
            return await send_json(w, sorted(res, key=lambda x: x["since"]))
        if seg[:1] == ["listeners"] and len(seg) == 3 and m == "POST":
            lid = int(seg[1])
            for st in self.stations.values():
                for h in st.hubs.values():
                    l = h.listeners.get(lid)
                    if l:
                        if seg[2] == "ban":
                            self.db.exec("INSERT OR REPLACE INTO bans(ip, reason, created) VALUES(?,?,?)",
                                         (l.ip, "ban manual", now()))
                            for h2 in st.hubs.values():
                                for l2 in list(h2.listeners.values()):
                                    if l2.ip == l.ip:
                                        l2.close()
                        l.close()
                        return await send_json(w, {"ok": True})
            raise HTTPError(404, "Ascultătorul nu mai e conectat")
        if seg == ["bans"]:
            if m == "GET":
                self.db.exec("DELETE FROM bans WHERE until > 0 AND until < ?", (now(),))
                return await send_json(w, self.db.all("SELECT * FROM bans ORDER BY created DESC"))
            d = await req.json()
            ip = (d.get("ip") or "").strip()
            if not ip:
                raise HTTPError(400, "IP lipsă")
            if m == "POST":
                self.db.exec("INSERT OR REPLACE INTO bans(ip, reason, created) VALUES(?,?,?)",
                             (ip, d.get("reason", ""), now()))
            elif m == "DELETE":
                self.db.exec("DELETE FROM bans WHERE ip=?", (ip,))
            return await send_json(w, {"ok": True})
        if seg == ["users"]:
            if m == "GET":
                return await send_json(w, self.db.all(
                    "SELECT u.id, u.username, u.role, u.station_id, u.created, s.name AS station FROM users u "
                    "LEFT JOIN stations s ON s.id=u.station_id ORDER BY u.role, u.username"))
            if m == "POST":
                d = await req.json()
                un = (d.get("username") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9._@\-]{3,40}", un):
                    raise HTTPError(400, "Utilizator: 3-40 caractere (litere, cifre, . _ - @)")
                if self.db.one("SELECT id FROM users WHERE username=?", (un,)):
                    raise HTTPError(409, "Utilizatorul există deja")
                role = d.get("role", "manager")
                if role not in ("admin", "manager"):
                    raise HTTPError(400, "Rol invalid")
                pw = d.get("password") or gen_password(10)
                if len(pw) < 6:
                    raise HTTPError(400, "Parola: minim 6 caractere")
                sid = int(d["station_id"]) if role == "manager" and d.get("station_id") else None
                if role == "manager" and not sid:
                    raise HTTPError(400, "Alege postul managerului")
                self.db.exec("INSERT INTO users(username, pass, role, station_id, created) VALUES(?,?,?,?,?)",
                             (un, hash_password(pw), role, sid, now()))
                return await send_json(w, {"ok": True, "password": pw})
        if seg[:1] == ["users"] and len(seg) == 2:
            uid = int(seg[1])
            if m == "DELETE":
                me = self.current_user(req)
                if me["id"] == uid:
                    raise HTTPError(400, "Nu îți poți șterge propriul cont")
                self.db.exec("DELETE FROM users WHERE id=?", (uid,))
                self.db.exec("DELETE FROM sessions WHERE user_id=?", (uid,))
                return await send_json(w, {"ok": True})
            if m == "PUT":
                d = await req.json()
                res = {"ok": True}
                if d.get("reset_password"):
                    pw = d.get("password") or gen_password(10)
                    self.db.exec("UPDATE users SET pass=? WHERE id=?", (hash_password(pw), uid))
                    self.db.exec("DELETE FROM sessions WHERE user_id=?", (uid,))
                    res["password"] = pw
                if "station_id" in d:
                    self.db.exec("UPDATE users SET station_id=? WHERE id=? AND role='manager'",
                                 (int(d["station_id"]) if d["station_id"] else None, uid))
                return await send_json(w, res)
        if seg == ["events"]:
            sid = req.query.get("station")
            args, where = [], ""
            if sid:
                where, args = "WHERE e.station_id=?", [int(sid)]
            ev = self.db.all(f"SELECT e.*, s.name AS station FROM events e LEFT JOIN stations s ON s.id=e.station_id "
                             f"{where} ORDER BY e.id DESC LIMIT 300", args)
            return await send_json(w, {"events": ev, "log": list(LOG)[-300:][::-1]})
        if seg == ["settings"]:
            keys = ["public_url", "timezone", "telegram_token", "telegram_chat", "retention_days"]
            if m == "GET":
                res = {k: self.db.get_setting(k, "") for k in keys}
                res["timezone"] = res["timezone"] or "Europe/Bucharest"
                res["retention_days"] = res["retention_days"] or "730"
                res["geoip_age_days"] = self.geo.age_days()
                res["port"] = self.port
                return await send_json(w, res)
            d = await req.json()
            for k in keys:
                if k in d:
                    v = str(d[k]).strip()
                    if k == "timezone":
                        try:
                            ZoneInfo(v)
                        except Exception:
                            raise HTTPError(400, "Fus orar invalid (ex: Europe/Bucharest)")
                    self.db.set_setting(k, v)
            return await send_json(w, {"ok": True})
        if seg == ["geoip-update"] and m == "POST":
            ok = await asyncio.to_thread(self.geo.update)
            return await send_json(w, {"ok": ok})
        if seg == ["test-alert"] and m == "POST":
            if not self.db.get_setting("telegram_token") or not self.db.get_setting("telegram_chat"):
                raise HTTPError(400, "Completează și salvează întâi token-ul și chat ID-ul Telegram")
            self.alert("✅ NtS-Radio: test alertă Telegram reușit.")
            return await send_json(w, {"ok": True})
        raise HTTPError(404, "Endpoint inexistent")

    async def api_files(self, req, cfg, st):
        m, w = req.method, req.writer
        kind = req.query.get("type", "music")
        if kind not in ("music", "jingles"):
            raise HTTPError(400)
        d = os.path.join(self.data_dir, "stations", cfg["slug"], kind)
        os.makedirs(d, exist_ok=True)
        if m == "GET":
            res = []
            for root, _, files in os.walk(d):
                for f in files:
                    fp = os.path.join(root, f)
                    if os.path.splitext(f)[1].lower() in {".mp3", ".aac", ".m4a", ".ogg", ".opus", ".flac",
                                                          ".wav", ".wma", ".mp2"}:
                        stt = os.stat(fp)
                        res.append({"name": os.path.relpath(fp, d), "size": stt.st_size, "mtime": int(stt.st_mtime)})
            res.sort(key=lambda x: x["name"].lower())
            free = shutil.disk_usage(d).free
            return await send_json(w, {"files": res, "free": free, "dir": d})
        name = req.query.get("name", "")
        if m == "POST":
            fname = safe_filename(name)
            if os.path.splitext(fname)[1].lower() not in {".mp3", ".aac", ".m4a", ".ogg", ".opus", ".flac", ".wav",
                                                           ".wma", ".mp2"}:
                raise HTTPError(400, "Format audio nesuportat")
            n = int(req.headers.get("content-length") or 0)
            if n <= 0 or n > 1024 * 1024 * 1024:
                raise HTTPError(413, "Fișier prea mare (max 1 GB)")
            tmp = os.path.join(d, f".upload-{secrets.token_hex(4)}")
            try:
                with open(tmp, "wb") as f:
                    left = n
                    while left > 0:
                        chunk = await req.reader.read(min(262144, left))
                        if not chunk:
                            raise HTTPError(400, "Upload întrerupt")
                        f.write(chunk)
                        left -= len(chunk)
                os.replace(tmp, os.path.join(d, fname))
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            if st:
                st.autodj.scan(force=True)
                st.prewarm_intro()
            return await send_json(w, {"ok": True, "name": fname})
        if m == "DELETE":
            fp = os.path.normpath(os.path.join(d, name))
            if not fp.startswith(os.path.normpath(d) + os.sep) or not os.path.isfile(fp):
                raise HTTPError(404)
            os.remove(fp)
            if st:
                st.autodj.scan(force=True)
                st.prewarm_intro()
            return await send_json(w, {"ok": True})
        raise HTTPError(405)

    def server_info(self):
        info = {"uptime": int(time.time() - self.started), "version": VERSION}
        try:
            info["load"] = os.getloadavg()
            info["cpus"] = os.cpu_count()
            with open("/proc/meminfo") as f:
                mi = {l.split(":")[0]: int(l.split()[1]) for l in f}
            info["mem_total"] = mi["MemTotal"] * 1024
            info["mem_used"] = (mi["MemTotal"] - mi.get("MemAvailable", mi["MemFree"])) * 1024
            du = shutil.disk_usage(self.data_dir)
            info["disk_total"], info["disk_free"] = du.total, du.free
        except Exception:
            pass
        kbps = 0
        for st in self.stations.values():
            for h in st.hubs.values():
                kbps += h.bitrate * len(h.listeners)
        info["out_kbps"] = kbps
        info["listeners"] = sum(st.listener_count() for st in self.stations.values())
        return info
