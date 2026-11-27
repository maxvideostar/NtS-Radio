#!/usr/bin/env python3
"""
nts-jamendo – adaugă zilnic în AutoDJ cele mai populare piese noi de pe Jamendo (Creative Commons).

Pentru posturi NECOMERCIALE: toate licențele Creative Commons permit difuzarea necomercială cu
atribuire (artist + titlu – NtS-Radio le afișează oricum în stream din tag-uri; scriptul ține și
un fișier CREDITE-jamendo.txt cu licența fiecărei piese).

Exemple:
  nts-jamendo.py --client-id ABC123 --dest /var/lib/nts-radio/stations/nts/music
  nts-jamendo.py --client-id ABC123 --dest ... --tag deephouse --count 5 --bitrate 128
  nts-jamendo.py --client-id ABC123 --dest ... --install-cron 05:30     # rulează zilnic
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

API = os.environ.get("JAMENDO_API", "https://api.jamendo.com/v3.0/tracks/")
UA = "NtS-Radio-Jamendo/1.0"
STATE = ".jamendo-state.json"
CREDITS = "CREDITE-jamendo.txt"


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", s).strip().strip(".")
    return re.sub(r"\s+", " ", s)[:150] or "piesa"


def api_get(params, retries=3):
    url = API + "?" + urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
                j = json.load(r)
            h = j.get("headers", {})
            if h.get("status") != "success":
                raise RuntimeError(f"Jamendo: {h.get('error_message') or h}")
            return j.get("results", [])
        except RuntimeError:
            raise
        except Exception as ex:
            if i == retries - 1:
                raise
            log(f"  reîncerc API ({ex})")
            time.sleep(5)


def download(url, path):
    tmp = path + ".part"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=120) as r, \
            open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 16)
    os.replace(tmp, path)


def license_name(url):
    m = re.search(r"/licenses/([a-z\-]+)/([\d.]+)", url or "")
    return f"CC {m.group(1).upper()} {m.group(2)}" if m else (url or "necunoscută")


def convert(src, dst, t, bitrate):
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-map", "0:a:0",
            "-map_metadata", "-1", "-vn"]
    if bitrate:
        args += ["-c:a", "libmp3lame", "-b:a", f"{bitrate}k", "-ar", "44100"]
    else:
        args += ["-c:a", "copy"]
    args += ["-id3v2_version", "3",
             "-metadata", f"title={t['name']}", "-metadata", f"artist={t['artist_name']}",
             "-metadata", f"album={t.get('album_name') or ''}",
             "-metadata", f"comment=Jamendo {license_name(t.get('license_ccurl'))} {t.get('shareurl') or ''}",
             dst]
    subprocess.run(args, check=True)


def chown_like(path, ref):
    try:
        st = os.stat(ref)
        os.chown(path, st.st_uid, st.st_gid)
    except Exception:
        pass


def install_cron(argv, at):
    hh, mm = (int(x) for x in at.split(":"))
    clean, skip = [], False
    for x in argv:                     # argumentele curente, fără --install-cron
        if skip:
            skip = False
            continue
        if x == "--install-cron":
            skip = True
            continue
        if x.startswith("--install-cron="):
            continue
        clean.append(x)
    cmd = " ".join(["/usr/bin/python3", os.path.abspath(__file__)] + [f"'{x}'" if " " in x else x for x in clean])
    with open("/etc/cron.d/nts-jamendo", "w") as f:
        f.write("# NtS-Radio – piese noi de pe Jamendo, zilnic\nSHELL=/bin/sh\n"
                f"{mm} {hh} * * * root {cmd} >> /var/log/nts-jamendo.log 2>&1\n")
    os.chmod("/etc/cron.d/nts-jamendo", 0o644)
    print(f"✅ Programat zilnic la {at}: /etc/cron.d/nts-jamendo\n   Jurnal: /var/log/nts-jamendo.log")


def main():
    ap = argparse.ArgumentParser(description="Piese Creative Commons de pe Jamendo → AutoDJ NtS-Radio")
    ap.add_argument("--client-id", default=os.environ.get("JAMENDO_CLIENT_ID"), help="client ID de pe devportal.jamendo.com")
    ap.add_argument("--dest", required=True, help="folderul de muzică al postului, ex. /var/lib/nts-radio/stations/nts/music")
    ap.add_argument("--tag", default="deephouse", help="gen/tag Jamendo (implicit deephouse; mai multe: deephouse+house)")
    ap.add_argument("--count", type=int, default=5, help="câte piese noi pe rulare (implicit 5)")
    ap.add_argument("--order", default="popularity_week",
                    help="popularity_week | popularity_month | popularity_total | listens_week | downloads_week | releasedate")
    ap.add_argument("--bitrate", type=int, default=128, help="MP3 kbps (implicit 128; 0 = păstrează originalul ~320 VBR)")
    ap.add_argument("--min-sec", type=int, default=120, help="durată minimă (sec)")
    ap.add_argument("--max-sec", type=int, default=720, help="durată maximă (sec) – exclude mixurile lungi")
    ap.add_argument("--subfolder", default="Jamendo", help="subfolder în --dest (gol = direct în dest)")
    ap.add_argument("--doar-ccby", action="store_true", help="doar licențe care permit și uz comercial (fără NC)")
    ap.add_argument("--fara-nd", action="store_true", help="exclude licențele No-Derivatives")
    ap.add_argument("--install-cron", metavar="HH:MM", help="programează rularea zilnică (necesită root)")
    a = ap.parse_args()

    if not a.client_id:
        sys.exit("Lipsește --client-id (gratuit pe https://devportal.jamendo.com → Create app)")
    if a.install_cron:
        if os.geteuid() != 0:
            sys.exit("--install-cron trebuie rulat cu sudo")
        install_cron(sys.argv[1:], a.install_cron)
        return
    if not shutil.which("ffmpeg"):
        sys.exit("Lipsește ffmpeg")
    if not os.path.isdir(a.dest):
        sys.exit(f"Folderul nu există: {a.dest}")

    out_dir = os.path.join(a.dest, a.subfolder) if a.subfolder else a.dest
    os.makedirs(out_dir, exist_ok=True)
    chown_like(out_dir, a.dest)
    state_path = os.path.join(out_dir, STATE)
    try:
        state = json.load(open(state_path))
    except Exception:
        state = {"ids": []}
    have = set(map(str, state["ids"]))

    params = {"client_id": a.client_id, "format": "json", "limit": 50, "order": a.order,
              "tags": a.tag, "audiodlformat": "mp32", "include": "licenses",
              "durationbetween": f"{a.min_sec}_{a.max_sec}"}
    if a.doar_ccby:
        params["ccnc"] = "false"
    if a.fara_nd:
        params["ccnd"] = "false"

    log(f"Caut {a.count} piese noi „{a.tag}” ({a.order}) …")
    got, offset, tmp_dir = 0, 0, os.path.join(out_dir, ".tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    while got < a.count and offset < 1000:
        params["offset"] = offset
        res = api_get(params)
        if not res:
            break
        offset += len(res)
        for t in res:
            if got >= a.count:
                break
            tid = str(t.get("id"))
            if tid in have:
                continue
            if not t.get("audiodownload_allowed") or not t.get("audiodownload"):
                continue
            name = safe_name(f"{t['artist_name']} - {t['name']}")
            dst = os.path.join(out_dir, name + ".mp3")
            if os.path.exists(dst):
                have.add(tid)
                continue
            src = os.path.join(tmp_dir, tid + ".src")
            try:
                download(t["audiodownload"], src)
                convert(src, dst + ".part.mp3", t, a.bitrate)
                os.replace(dst + ".part.mp3", dst)
                chown_like(dst, a.dest)
            except Exception as ex:
                log(f"  ✗ {name}: {ex}")
                for p in (src, dst + ".part.mp3"):
                    if os.path.exists(p):
                        os.remove(p)
                continue
            finally:
                if os.path.exists(src):
                    os.remove(src)
            got += 1
            have.add(tid)
            lic = license_name(t.get("license_ccurl"))
            log(f"  ✅ {name}  [{lic}]")
            with open(os.path.join(out_dir, CREDITS), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d')}\t{t['artist_name']} – {t['name']}\t{lic}\t"
                        f"{t.get('license_ccurl', '')}\t{t.get('shareurl', '')}\n")
            chown_like(os.path.join(out_dir, CREDITS), a.dest)
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass
    state["ids"] = sorted(have, key=lambda x: int(x) if x.isdigit() else 0)
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(state_path, "w") as f:
        json.dump(state, f)
    chown_like(state_path, a.dest)
    log(f"Gata: {got} piese noi în {out_dir}" + ("" if got >= a.count else " (nu am găsit mai multe noi)"))


if __name__ == "__main__":
    main()
