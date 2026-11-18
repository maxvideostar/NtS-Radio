"""Înregistrări: rulează nts-rip.py (contrib/recorder) pentru fiecare stream configurat în panou."""
import asyncio
import collections
import os
import re
import sys
import time

from .util import log, now, slugify

AUDIO = (".mp3", ".aac", ".ogg", ".m4a")


class RecJob:
    def __init__(self, mgr, cfg):
        self.mgr = mgr
        self.cfg = cfg
        self.id = cfg["id"]
        self.proc = None
        self.task = None
        self.lines = collections.deque(maxlen=300)
        self.state = "off"
        self.current = None
        self.current_since = None
        self.saved = 0
        self.error = ""
        self.started = None

    @property
    def dir(self):
        return os.path.join(self.mgr.base, f"{self.id}-{slugify(self.cfg['name'])}")

    def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        t = self.task
        self.task = None
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()                       # nts-rip salvează curat la SIGTERM
                await asyncio.wait_for(self.proc.wait(), 8)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if t:
            t.cancel()
        self.state = "off"
        self.current = None

    async def run(self):
        c = self.cfg
        os.makedirs(self.dir, exist_ok=True)
        while True:
            args = [sys.executable, "-u", self.mgr.script, c["url"], "-o", self.dir,
                    "--min", str(c.get("min_sec") or 60), "--offset", str(c.get("offset_sec") or 0)]
            if c.get("continuu"):
                args.append("--continuu")
            for ig in [x.strip() for x in (c.get("ignore_list") or "").split(",") if x.strip()]:
                args += ["--ignora", ig]
            self.state = "connecting"
            self.started = time.time()
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                while True:
                    line = await self.proc.stdout.readline()
                    if not line:
                        break
                    self.parse(line.decode("utf-8", "replace").rstrip())
                await self.proc.wait()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.lines.append(f"eroare: {e}")
            self.state = "error"
            self.error = self.lines[-1] if self.lines else "procesul s-a oprit"
            self.current = None
            await asyncio.sleep(10)

    def parse(self, line):
        self.lines.append(line)
        msg = line[9:] if re.match(r"\d\d:\d\d:\d\d ", line) else line
        if msg.startswith("conectat"):
            self.state = "recording"
            self.error = ""
        elif msg.startswith("⏺"):
            self.current = msg[1:].strip()
            self.current_since = time.time()
        elif msg.startswith("⏭"):
            self.current = None
        elif msg.startswith("✅"):
            self.saved += 1
        elif msg.startswith("conexiune pierdută") or msg.startswith("ATENȚIE"):
            self.state = "error" if msg.startswith("conexiune") else self.state
            self.error = msg

    def files(self):
        res = []
        if not os.path.isdir(self.dir):
            return res
        durs = {}
        tl = os.path.join(self.dir, "tracklist.txt")
        if os.path.isfile(tl):
            with open(tl, encoding="utf-8", errors="replace") as f:
                for l in f:
                    p = l.rstrip("\n").split("\t")
                    if len(p) == 3:
                        durs[p[2]] = p[1]
        for fn in os.listdir(self.dir):
            if fn.startswith(".") or not fn.lower().endswith(AUDIO):
                continue
            st = os.stat(os.path.join(self.dir, fn))
            base = os.path.splitext(fn)[0]
            dur = durs.get(base) or durs.get(re.sub(r" \(\d+\)$", "", base), "")
            res.append({"name": fn, "size": st.st_size, "mtime": int(st.st_mtime),
                        "duration": dur, "full": fn.startswith("emisie-")})
        res.sort(key=lambda x: -x["mtime"])
        return res

    def info(self):
        size = sum(f["size"] for f in self.files())
        return {**self.cfg, "state": self.state if self.task else "off", "current": self.current,
                "current_since": int(self.current_since) if self.current and self.current_since else None,
                "saved": self.saved, "error": self.error, "size": size,
                "count": len([f for f in self.files() if not f["full"]]),
                "since": int(self.started) if self.started and self.task else None}

    def cleanup(self):
        days = int(self.cfg.get("keep_days") or 0)
        if days <= 0 or not os.path.isdir(self.dir):
            return
        lim = time.time() - days * 86400
        for fn in os.listdir(self.dir):
            p = os.path.join(self.dir, fn)
            if fn.lower().endswith(AUDIO) and os.path.getmtime(p) < lim:
                try:
                    os.remove(p)
                except OSError:
                    pass


class Recorder:
    def __init__(self, radio):
        self.radio = radio
        self.db = radio.db
        self.base = os.path.join(radio.data_dir, "recordings")
        self.script = os.path.join(os.path.dirname(radio.web_dir), "contrib", "recorder", "nts-rip.py")
        self.jobs = {}
        self.db.exec("""CREATE TABLE IF NOT EXISTS rec_jobs (id INTEGER PRIMARY KEY, name TEXT, url TEXT,
            min_sec INTEGER DEFAULT 60, offset_sec REAL DEFAULT 0, continuu INTEGER DEFAULT 0, keep_days INTEGER DEFAULT 0,
            ignore_list TEXT DEFAULT '', enabled INTEGER DEFAULT 1, created INTEGER)""")

    def start_all(self):
        for cfg in self.db.all("SELECT * FROM rec_jobs ORDER BY id"):
            j = RecJob(self, cfg)
            self.jobs[cfg["id"]] = j
            if cfg["enabled"]:
                j.start()
                log(f"înregistrare pornită: {cfg['name']}")

    def reload(self, jid):
        cfg = self.db.one("SELECT * FROM rec_jobs WHERE id=?", (jid,))
        j = self.jobs.get(jid)
        if j:
            j.cfg = cfg
        elif cfg:
            j = self.jobs[jid] = RecJob(self, cfg)
        return j

    def cleanup(self):
        for j in self.jobs.values():
            j.cleanup()
