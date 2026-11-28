"""NtS-Radio – detector de calitate pentru biblioteca AutoDJ.

Pentru fiecare piesă se analizează ~20 s din mijloc: energia peste 11/15/16/17/18/19/20 kHz,
relativ la banda de 11 kHz. Encoderele cu pierderi taie înaltele la o frecvență care trădează
bitrate-ul real (128 kbps / YouTube ≈ 16 kHz, 192 ≈ 18 kHz, 320 ≈ 19-20 kHz), indiferent ce
scrie în fișier (un MP3 de 320 kbps făcut dintr-un rip YouTube tot la 16 kHz e tăiat).
"""
import asyncio
import json
import os
import re
import shutil
import time

from .util import log

BANDS = [11000, 15000, 16000, 17000, 18000, 19000, 20000]
AUDIO_EXT = {".mp3", ".aac", ".m4a", ".ogg", ".opus", ".flac", ".wav", ".wma", ".mp2"}
SCHEMA = """CREATE TABLE IF NOT EXISTS file_quality(
  station_id INTEGER, path TEXT, size INTEGER, mtime INTEGER,
  bitrate INTEGER, codec TEXT, cutoff INTEGER, grade TEXT, analyzed INTEGER,
  PRIMARY KEY(station_id, path))"""

GRADES = {
    "good": "bună",          # ≥ 19 kHz – sursă de calitate (CD / 256-320 kbps)
    "ok": "acceptabilă",     # ~18 kHz – ~192 kbps
    "poor": "slabă",         # 16-17 kHz – 128 kbps / rip YouTube
    "bad": "foarte slabă",   # ≤ 15 kHz – 96 kbps sau mai puțin
    "unknown": "nedeterminată",
}


def grade_for(cutoff):
    if cutoff is None:
        return "unknown"
    if cutoff >= 19000:
        return "good"
    if cutoff >= 18000:
        return "ok"
    if cutoff >= 16000:
        return "poor"
    return "bad"


class Quality:
    def __init__(self, radio):
        self.radio = radio
        self.db = radio.db
        self.db.conn.execute(SCHEMA)
        self.db.conn.commit()
        self.task = None
        self.busy = None             # fișierul analizat acum
        self.nice = shutil.which("nice")

    def start(self):
        self.task = asyncio.create_task(self.loop())

    # ---------- analiză ----------
    async def _run(self, args, timeout=60):
        if self.nice:
            args = [self.nice, "-n", "15"] + args
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            p.kill()
            return b"", b""
        return out, err

    async def analyze(self, path):
        from .engine import FFMPEG, FFPROBE
        out, _ = await self._run([FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format",
                                  "-show_streams", "-select_streams", "a:0", path], 20)
        try:
            j = json.loads(out or b"{}")
            dur = float(j.get("format", {}).get("duration") or 0)
            st = (j.get("streams") or [{}])[0]
            codec = st.get("codec_name") or ""
            br = int(st.get("bit_rate") or j.get("format", {}).get("bit_rate") or 0) // 1000
        except Exception:
            dur, codec, br = 0, "", 0
        start = max(0.0, dur * 0.35) if dur > 40 else 0
        n = len(BANDS) + 1
        fc = f"[0:a]aformat=channel_layouts=mono,asplit={n}" + "".join(f"[s{i}]" for i in range(n)) + ";"
        fc += "[s0]astats=measure_perchannel=none:measure_overall=RMS_level[o0]"
        for i, f in enumerate(BANDS, 1):
            fc += f";[s{i}]" + ",".join([f"highpass=f={f}:p=2"] * 4) + \
                  f",astats=measure_perchannel=none:measure_overall=RMS_level[o{i}]"
        args = [FFMPEG, "-hide_banner", "-nostats", "-ss", f"{start:.1f}", "-t", "20", "-i", path,
                "-filter_complex", fc]
        for i in range(n):
            args += ["-map", f"[o{i}]", "-f", "null", "-"]
        _, err = await self._run(args, 90)
        vals, cur = {}, None
        for line in err.decode("utf-8", "replace").splitlines():
            m = re.search(r"Parsed_astats_(\d+)", line)
            if m:
                cur = int(m.group(1))
            m2 = re.search(r"RMS level dB:\s*(-?[\d.]+|-inf)", line)
            if m2 and cur is not None and cur not in vals:
                vals[cur] = float(m2.group(1)) if m2.group(1) != "-inf" else -200.0
        cutoff = None
        if len(vals) == n:
            k = sorted(vals)
            tot = vals[k[0]]
            rel = [vals[i] - tot for i in k[1:]]          # rel[0] = 11 kHz
            if rel[0] > -45:                                # piesa are destule înalte ca să putem judeca
                cutoff = BANDS[0]
                for f, r in zip(BANDS[1:], rel[1:]):
                    if r - rel[0] > -20:
                        cutoff = f
        return {"bitrate": br, "codec": codec, "cutoff": cutoff, "grade": grade_for(cutoff)}

    # ---------- buclă de fundal ----------
    def music_dir(self, slug):
        return os.path.join(self.radio.data_dir, "stations", slug, "music")

    @staticmethod
    def _scan(d):
        """parcurgerea discului – rulează într-un thread separat, nu blochează audio"""
        res = []
        for root, _, files in os.walk(d):
            for f in files:
                if os.path.splitext(f)[1].lower() not in AUDIO_EXT:
                    continue
                fp = os.path.join(root, f)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                res.append((os.path.relpath(fp, d), fp, st.st_size, int(st.st_mtime)))
        return res

    async def pending(self):
        """(station_id, rel, abs, size, mtime) pentru fișierele neanalizate / modificate"""
        out = []
        for cfg in self.db.stations():
            d = self.music_dir(cfg["slug"])
            if not os.path.isdir(d):
                continue
            known = {r["path"]: r for r in self.db.all(
                "SELECT path, size, mtime FROM file_quality WHERE station_id=?", (cfg["id"],))}
            seen = set()
            for rel, fp, size, mtime in await asyncio.to_thread(self._scan, d):
                seen.add(rel)
                k = known.get(rel)
                if not k or k["size"] != size or k["mtime"] != mtime:
                    out.append((cfg["id"], rel, fp, size, mtime))
            gone = set(known) - seen
            for g in gone:
                self.db.conn.execute("DELETE FROM file_quality WHERE station_id=? AND path=?", (cfg["id"], g))
            if gone:
                self.db.conn.commit()
        return out

    async def loop(self):
        await asyncio.sleep(20)
        while True:
            try:
                todo = await self.pending()
                for sid, rel, fp, size, mtime in todo:
                    self.busy = rel
                    r = await self.analyze(fp)
                    self.db.conn.execute(
                        "INSERT OR REPLACE INTO file_quality(station_id, path, size, mtime, bitrate, codec, cutoff,"
                        " grade, analyzed) VALUES(?,?,?,?,?,?,?,?,?)",
                        (sid, rel, size, mtime, r["bitrate"], r["codec"], r["cutoff"], r["grade"], int(time.time())))
                    self.db.conn.commit()
                    await asyncio.sleep(0.3)          # nu încărcăm serverul
                self.busy = None
                if todo:
                    log(f"calitate: {len(todo)} piese analizate", "info")
            except asyncio.CancelledError:
                break
            except Exception as ex:
                log(f"calitate: {ex!r}", "error")
                self.busy = None
            await asyncio.sleep(60)

    def for_station(self, sid):
        return {r["path"]: r for r in self.db.all(
            "SELECT path, bitrate, codec, cutoff, grade FROM file_quality WHERE station_id=?", (sid,))}

    def reset(self, sid, path=None):
        if path:
            self.db.conn.execute("DELETE FROM file_quality WHERE station_id=? AND path=?", (sid, path))
        else:
            self.db.conn.execute("DELETE FROM file_quality WHERE station_id=?", (sid,))
        self.db.conn.commit()
