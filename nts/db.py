import json
import os
import sqlite3
import threading

from .util import now

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pass TEXT NOT NULL,
  role TEXT NOT NULL, station_id INTEGER, created INTEGER);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER, created INTEGER, last INTEGER);
CREATE TABLE IF NOT EXISTS stations (
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  genre TEXT DEFAULT '', description TEXT DEFAULT '', website TEXT DEFAULT '',
  source_pass TEXT NOT NULL, max_listeners INTEGER DEFAULT 1000, enabled INTEGER DEFAULT 1,
  autodj INTEGER DEFAULT 1, shuffle INTEGER DEFAULT 1, crossfade REAL DEFAULT 3,
  jingle_every INTEGER DEFAULT 4, silence_secs INTEGER DEFAULT 0,
  outputs TEXT NOT NULL, logo TEXT DEFAULT '', created INTEGER);
CREATE TABLE IF NOT EXISTS listeners (
  id INTEGER PRIMARY KEY, station_id INTEGER, mount TEXT, ip TEXT, ua TEXT, country TEXT,
  started INTEGER, ended INTEGER, duration INTEGER);
CREATE INDEX IF NOT EXISTS ix_lis_st ON listeners(station_id, started);
CREATE INDEX IF NOT EXISTS ix_lis_end ON listeners(station_id, ended);
CREATE TABLE IF NOT EXISTS snapshots (station_id INTEGER, ts INTEGER, listeners INTEGER, live INTEGER,
  PRIMARY KEY(station_id, ts));
CREATE TABLE IF NOT EXISTS songs (id INTEGER PRIMARY KEY, station_id INTEGER, ts INTEGER, title TEXT,
  source TEXT, listeners INTEGER);
CREATE INDEX IF NOT EXISTS ix_songs ON songs(station_id, ts);
CREATE TABLE IF NOT EXISTS shows (id INTEGER PRIMARY KEY, station_id INTEGER, name TEXT, host TEXT,
  days TEXT, start TEXT, end TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, station_id INTEGER, ts INTEGER, type TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_events ON events(station_id, ts);
CREATE TABLE IF NOT EXISTS bans (ip TEXT PRIMARY KEY, reason TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS playlists (id INTEGER PRIMARY KEY, station_id INTEGER, name TEXT, shuffle INTEGER DEFAULT 0,
  created INTEGER);
CREATE TABLE IF NOT EXISTS playlist_items (id INTEGER PRIMARY KEY, playlist_id INTEGER, path TEXT, pos INTEGER);
CREATE INDEX IF NOT EXISTS ix_pli ON playlist_items(playlist_id, pos);
CREATE TABLE IF NOT EXISTS schedule (id INTEGER PRIMARY KEY, station_id INTEGER, playlist_id INTEGER, days TEXT,
  start TEXT, end TEXT, pos INTEGER);
"""

DEFAULT_OUTPUTS = [
    {"mount": "", "codec": "mp3", "bitrate": 128},
    {"mount": ".aac", "codec": "aac", "bitrate": 64},
]


class DB:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA synchronous=NORMAL")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(stations)")}
        if "relay" not in cols:
            self.conn.execute("ALTER TABLE stations ADD COLUMN relay TEXT DEFAULT '{}'")
        if "pushes" not in cols:
            self.conn.execute("ALTER TABLE stations ADD COLUMN pushes TEXT DEFAULT '[]'")
        if "default_playlist" not in cols:
            self.conn.execute("ALTER TABLE stations ADD COLUMN default_playlist INTEGER DEFAULT 0")
        if "intro" not in cols:
            self.conn.execute("ALTER TABLE stations ADD COLUMN intro TEXT DEFAULT ''")
        if "source_ips" not in cols:
            self.conn.execute("ALTER TABLE stations ADD COLUMN source_ips TEXT DEFAULT ''")
        bcols = {r[1] for r in self.conn.execute("PRAGMA table_info(bans)")}
        if "until" not in bcols:
            self.conn.execute("ALTER TABLE bans ADD COLUMN until INTEGER DEFAULT 0")
        if "hits" not in bcols:
            self.conn.execute("ALTER TABLE bans ADD COLUMN hits INTEGER DEFAULT 0")
            self.conn.execute("ALTER TABLE bans ADD COLUMN last_seen INTEGER DEFAULT 0")
            # trecerea la blocare permanentă: blocările automate active devin permanente
            self.conn.execute("UPDATE bans SET until=0 WHERE until>0 AND reason LIKE 'auto:%'")

    def exec(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid

    def all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    def reader(self):
        """conexiune separată pentru interogări grele (rulate în thread)"""
        c = sqlite3.connect(self.path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    # ---- setări ----
    def get_setting(self, key, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set_setting(self, key, value):
        self.exec("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))

    # ---- posturi ----
    def stations(self):
        rows = self.all("SELECT * FROM stations ORDER BY id")
        for r in rows:
            self._decode(r)
        return rows

    @staticmethod
    def _decode(r):
        r["outputs"] = json.loads(r.get("outputs") or "[]")
        r["relay"] = json.loads(r.get("relay") or "{}")
        r["pushes"] = json.loads(r.get("pushes") or "[]")
        return r

    def station(self, sid):
        r = self.one("SELECT * FROM stations WHERE id=?", (sid,))
        return self._decode(r) if r else None

    def close_dangling_sessions(self):
        """la pornire: închide sesiunile rămase deschise după un restart/crash"""
        rows = self.all("SELECT id, station_id, started FROM listeners WHERE ended IS NULL")
        for r in rows:
            last = self.one("SELECT MAX(ts) AS t FROM snapshots WHERE station_id=?", (r["station_id"],))
            end = max(r["started"], (last or {}).get("t") or r["started"])
            self.exec("UPDATE listeners SET ended=?, duration=? WHERE id=?", (end, end - r["started"], r["id"]))

    def event(self, station_id, type_, detail=""):
        self.exec("INSERT INTO events(station_id, ts, type, detail) VALUES(?,?,?,?)",
                  (station_id, now(), type_, detail))
