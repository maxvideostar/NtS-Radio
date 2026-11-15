# NtS-Radio

Server de streaming pentru radio (tip SHOUTcast / Icecast), cu AutoDJ, panou de administrare și pagină de statistici pentru managerul postului.

- **Surse live din studio:** Icecast (SOURCE/PUT) și SHOUTcast v1. Merge cu RadioBOSS, SAM Broadcaster, Mixxx, BUTT, mixere cu encoder etc.
- **Comutare automată:** studio live → AutoDJ → liniște. Ascultătorii nu sunt deconectați la comutare.
- **AutoDJ:** shuffle, crossfade, jingle-uri la fiecare N piese, upload din browser.
- **Detectare liniște:** dacă studioul e conectat, dar nu se aude nimic N secunde, trece pe AutoDJ.
- **Ieșiri multiple:** MP3 / AAC la orice bitrate, cu metadate ICY (titlul piesei în player).
- **Admin:** dashboard live cu nivel audio, ascultători (kick / ban), posturi, AutoDJ, utilizatori, evenimente, alerte Telegram.
- **Pagina managerului (/manager):** ascultători live, TLH, ascultători unici, vârf, medie, profil pe ore și zile, țări, dispozitive, istoric și top piese, audiența pe emisiuni, rapoarte PDF / CSV, linkuri player.
- **Player public** (`/s/<post>`) și player embed (`/embed/<post>`).
- **Compatibilitate:** `/7.html`, `/stats?json=1`, `/status-json.xsl`, playlist `.m3u` / `.pls`.
- Pentru mai multe posturi pe același server, fiecare post are managerul lui.

Rulează pe Python 3 (doar biblioteca standard), FFmpeg și SQLite. Nu trebuie compilat nimic.

## Instalare (Ubuntu 22.04 / 24.04)

```bash
sudo git clone https://github.com/maxvideostar/NtS-Radio.git /opt/nts-radio
cd /opt/nts-radio && sudo bash install.sh
```

Repo privat: `sudo git clone https://<TOKEN>@github.com/maxvideostar/NtS-Radio.git /opt/nts-radio`

La final, scriptul afișează adresa panoului și parola contului `admin`.

Pentru alt port decât 8000: `sudo NTS_PORT=8010 bash install.sh`

## Actualizare

```bash
cd /opt/nts-radio && sudo git pull && sudo bash install.sh
```

## Porturi

| Port | Rol |
|------|-----|
| 8000/tcp | ascultători, panou web, surse Icecast |
| 8001/tcp | surse SHOUTcast v1 (encoderul îl folosește automat) |

## Date pentru studio

Le găsești în admin → **Posturi** → **Date studio**.

- **Icecast:** server = IP/domeniu, port 8000, mount `/radio`, utilizator `source`, parola sursei
- **SHOUTcast v1:** server = IP/domeniu, port 8000, parola sursei

## Fișiere

- Configurare: `/etc/nts-radio.env`
- Date (bază de date, muzică, logo): `/var/lib/nts-radio`
- Muzica AutoDJ se poate urca și prin SFTP în `/var/lib/nts-radio/stations/<post>/music` (și `jingles`)
- Jurnal: `journalctl -u nts-radio -f`

## HTTPS pe domeniu

Vezi `contrib/nginx-nts-radio.conf`. Studioul rămâne conectat direct pe 8000 / 8001.
