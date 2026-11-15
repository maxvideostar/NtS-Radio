"""Calcul statistici (rulează într-un thread separat, pe o conexiune SQLite proprie)."""
import collections
import datetime as dt
import time
from zoneinfo import ZoneInfo

from .util import classify_ua

MIN_LISTEN = 30   # sesiunile mai scurte de 30 s nu se numără ca "ascultare" (dar intră în TLH)


def day_starts(frm, to, tz):
    d = dt.datetime.fromtimestamp(frm, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    res = []
    while d.timestamp() < to:
        res.append((d.strftime("%Y-%m-%d"), int(d.timestamp())))
        d = (d + dt.timedelta(days=1, hours=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    return res


def overlap(s, e, a, b):
    return max(0, min(e, b) - max(s, a))


def _sessions(c, sid, frm, to):
    t = int(time.time())
    return c.execute(
        "SELECT ip, ua, country, mount, started, COALESCE(ended, ?) AS ended FROM listeners "
        "WHERE station_id=? AND started < ? AND COALESCE(ended, ?) > ?", (t, sid, to, t, frm)).fetchall()


def _snaps(c, sid, frm, to):
    return c.execute("SELECT ts, listeners FROM snapshots WHERE station_id=? AND ts>=? AND ts<? ORDER BY ts",
                     (sid, frm, to)).fetchall()


def summary(c, sid, frm, to):
    ses = _sessions(c, sid, frm, to)
    tlh = sum(overlap(r["started"], r["ended"], frm, to) for r in ses) / 3600
    real = [r for r in ses if r["ended"] - r["started"] >= MIN_LISTEN]
    snaps = _snaps(c, sid, frm, to)
    vals = [s["listeners"] for s in snaps]
    return {"tlh": round(tlh, 1), "unique": len({r["ip"] for r in real}), "sessions": len(real),
            "avg": round(sum(vals) / len(vals), 1) if vals else 0, "peak": max(vals) if vals else 0}


def period(c, sid, frm, to, tzname, shows):
    tz = ZoneInfo(tzname)
    ses = _sessions(c, sid, frm, to)
    snaps = _snaps(c, sid, frm, to)
    real = [r for r in ses if r["ended"] - r["started"] >= MIN_LISTEN]
    tlh_sec = sum(overlap(r["started"], r["ended"], frm, to) for r in ses)
    vals = [s["listeners"] for s in snaps]
    peak, peak_ts = 0, None
    for s in snaps:
        if s["listeners"] > peak:
            peak, peak_ts = s["listeners"], s["ts"]
    durs = sorted(r["ended"] - r["started"] for r in real)

    # --- serie de timp ---
    span = to - frm
    step = 600 if span <= 36 * 3600 else (3600 if span <= 15 * 86400 else 86400)
    buckets = collections.OrderedDict()
    if step == 86400:
        for label, start in day_starts(frm, to, tz):
            buckets[start] = []
        keys = list(buckets)
        for s in snaps:
            k = None
            for kk in keys:
                if kk <= s["ts"]:
                    k = kk
                else:
                    break
            if k is not None:
                buckets[k].append(s["listeners"])
    else:
        for t in range(frm - frm % step, to, step):
            buckets[t] = []
        for s in snaps:
            k = s["ts"] - s["ts"] % step
            if k in buckets:
                buckets[k].append(s["listeners"])
    series = [{"t": k, "avg": round(sum(v) / len(v), 1) if v else 0, "max": max(v) if v else 0}
              for k, v in buckets.items()]

    # --- profil pe ore / zile ale săptămânii ---
    by_hour = collections.defaultdict(list)
    by_wd = collections.defaultdict(list)
    for s in snaps:
        d = dt.datetime.fromtimestamp(s["ts"], tz)
        by_hour[d.hour].append(s["listeners"])
        by_wd[d.weekday()].append(s["listeners"])
    hours = [round(sum(by_hour[h]) / len(by_hour[h]), 1) if by_hour[h] else 0 for h in range(24)]
    weekdays = [round(sum(by_wd[w]) / len(by_wd[w]), 1) if by_wd[w] else 0 for w in range(7)]

    # --- pe zile ---
    days = day_starts(frm, to, tz)
    daily = []
    for i, (label, start) in enumerate(days):
        end = days[i + 1][1] if i + 1 < len(days) else to
        a, b = max(start, frm), min(end, to)
        t_sec = sum(overlap(r["started"], r["ended"], a, b) for r in ses)
        ips = {r["ip"] for r in real if r["started"] < b and r["ended"] > a}
        v = [s["listeners"] for s in snaps if a <= s["ts"] < b]
        daily.append({"day": label, "tlh": round(t_sec / 3600, 1), "unique": len(ips),
                      "avg": round(sum(v) / len(v), 1) if v else 0, "peak": max(v) if v else 0})

    # --- geografie, dispozitive, mounturi ---
    countries = collections.defaultdict(lambda: {"ips": set(), "tlh": 0})
    devices = collections.Counter()
    oses = collections.Counter()
    players = collections.Counter()
    mounts = collections.defaultdict(lambda: {"sessions": 0, "tlh": 0})
    for r in ses:
        o = overlap(r["started"], r["ended"], frm, to)
        cc = r["country"] or "?"
        countries[cc]["tlh"] += o
        mounts[r["mount"]]["tlh"] += o
    for r in real:
        countries[r["country"] or "?"]["ips"].add(r["ip"])
        dev, os_, pl = classify_ua(r["ua"])
        devices[dev] += 1
        oses[os_] += 1
        players[pl] += 1
        mounts[r["mount"]]["sessions"] += 1
    total_tlh = tlh_sec or 1
    country_list = sorted(({"code": k, "unique": len(v["ips"]), "tlh": round(v["tlh"] / 3600, 1),
                            "pct": round(100 * v["tlh"] / total_tlh, 1)} for k, v in countries.items()),
                          key=lambda x: -x["tlh"])

    # --- emisiuni ---
    show_stats = []
    for sh in shows:
        try:
            wd = {int(x) for x in str(sh["days"]).split(",") if x.strip() != ""}
            h1, m1 = map(int, sh["start"].split(":"))
            h2, m2 = map(int, sh["end"].split(":"))
        except Exception:
            continue
        occ, vals_s, t_sec = 0, [], 0
        for label, start in days:
            d = dt.datetime.fromtimestamp(start, tz)
            if d.isoweekday() not in wd:
                continue
            s0 = d.replace(hour=h1, minute=m1)
            e0 = d.replace(hour=h2, minute=m2)
            if e0 <= s0:
                e0 += dt.timedelta(days=1)
            a, b = int(s0.timestamp()), int(e0.timestamp())
            if a >= to or b <= frm or a > time.time():
                continue
            occ += 1
            v = [s["listeners"] for s in snaps if a <= s["ts"] < b]
            vals_s += v
            t_sec += sum(overlap(r["started"], r["ended"], a, b) for r in ses)
        show_stats.append({"id": sh["id"], "name": sh["name"], "host": sh.get("host") or "",
                           "days": sh["days"], "start": sh["start"], "end": sh["end"], "occurrences": occ,
                           "avg": round(sum(vals_s) / len(vals_s), 1) if vals_s else 0,
                           "peak": max(vals_s) if vals_s else 0, "tlh": round(t_sec / 3600, 1)})

    prev = summary(c, sid, frm - span, frm)
    return {
        "from": frm, "to": to, "step": step,
        "tlh": round(tlh_sec / 3600, 1), "unique": len({r["ip"] for r in real}), "sessions": len(real),
        "avg_listeners": round(sum(vals) / len(vals), 1) if vals else 0, "peak": peak, "peak_ts": peak_ts,
        "avg_duration": int(sum(durs) / len(durs)) if durs else 0,
        "median_duration": durs[len(durs) // 2] if durs else 0,
        "series": series, "hours": hours, "weekdays": weekdays, "daily": daily,
        "countries": country_list,
        "devices": devices.most_common(), "os": oses.most_common(), "players": players.most_common(12),
        "mounts": sorted(({"mount": k, "sessions": v["sessions"], "tlh": round(v["tlh"] / 3600, 1)}
                          for k, v in mounts.items()), key=lambda x: -x["tlh"]),
        "shows": show_stats, "prev": prev,
    }


def top_songs(c, sid, frm, to, limit=50):
    rows = c.execute("SELECT title, COUNT(*) AS plays, ROUND(AVG(listeners),1) AS avg_listeners, MAX(ts) AS last "
                     "FROM songs WHERE station_id=? AND ts>=? AND ts<? AND source!='off' AND title NOT LIKE '% - LIVE' "
                     "GROUP BY title ORDER BY plays DESC, avg_listeners DESC LIMIT ?", (sid, frm, to, limit)).fetchall()
    return [dict(r) for r in rows]


def history(c, sid, frm, to, limit=200, offset=0, q=""):
    args = [sid, frm, to]
    extra = ""
    if q:
        extra = " AND title LIKE ?"
        args.append(f"%{q}%")
    rows = c.execute(f"SELECT ts, title, source, listeners FROM songs WHERE station_id=? AND ts>=? AND ts<?{extra} "
                     f"ORDER BY ts DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
    total = c.execute(f"SELECT COUNT(*) FROM songs WHERE station_id=? AND ts>=? AND ts<?{extra}", args).fetchone()[0]
    return {"total": total, "rows": [dict(r) for r in rows]}
