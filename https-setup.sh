#!/usr/bin/env bash
# =====================================================================
#  NtS-Radio – HTTPS prin nginx + certificat Let's Encrypt (automat)
#
#  Utilizare:
#    cd /opt/nts-radio && sudo bash https-setup.sh domeniu.ro [email]
#
#  - merge cu nginx din apt SAU compilat din sursă (cu nginx-rtmp-module)
#  - dacă domeniul e deja folosit de alt site în nginx (ex. HLS pe s3.netstreaming.eu),
#    NU îl atinge: pune NtS-Radio pe un port HTTPS separat (implicit 8443) cu același certificat.
#    Alt port:  sudo HTTPS_PORT=9443 bash https-setup.sh ...
#  - reînnoire automată a certificatului + reload nginx
#  - studioul se conectează în continuare pe porturile NtS-Radio (8000 / 8001)
# =====================================================================
set -e
DOMAIN="$1"
EMAIL="$2"
[ "$(id -u)" -eq 0 ] || { echo "Rulează cu sudo."; exit 1; }
[ -n "$DOMAIN" ] || { echo "Utilizare: sudo bash https-setup.sh domeniu.ro [email]"; exit 1; }

ENV_FILE=/etc/nts-radio.env
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
PORT="${NTS_PORT:-8000}"
DATA="${NTS_DATA:-/var/lib/nts-radio}"
WEBROOT=/var/www/letsencrypt
if [ -n "$EMAIL" ]; then EM=(-m "$EMAIL"); else EM=(--register-unsafely-without-email); fi

echo "==> Domeniu: $DOMAIN  ->  NtS-Radio pe 127.0.0.1:$PORT"

# ---------- DNS ----------
MYIP=$(curl -4 -fs --max-time 5 https://api.ipify.org || true)
DNSIP=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')
if [ -z "$DNSIP" ]; then
  echo "EROARE: $DOMAIN nu are înregistrare DNS (A). Pune A record către ${MYIP:-IP-ul serverului}."
  exit 1
fi
if [ -n "$MYIP" ] && [ "$DNSIP" != "$MYIP" ]; then
  echo "ATENȚIE: $DOMAIN -> $DNSIP, dar serverul are IP public $MYIP. Continui..."
fi

# ---------- nginx ----------
NGINX=$(command -v nginx || true)
[ -z "$NGINX" ] && [ -x /usr/local/nginx/sbin/nginx ] && NGINX=/usr/local/nginx/sbin/nginx
if [ -z "$NGINX" ]; then
  echo "==> nginx nu e instalat – îl instalez din apt"
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx >/dev/null
  NGINX=$(command -v nginx)
fi
"$NGINX" -V 2>&1 | grep -q "http_ssl_module" || { echo "EROARE: nginx fără modul SSL (--with-http_ssl_module)."; exit 1; }
CONF=$("$NGINX" -V 2>&1 | grep -o -- '--conf-path=[^ ]*' | cut -d= -f2)
[ -n "$CONF" ] && [ -f "$CONF" ] || CONF=$("$NGINX" -t 2>&1 | grep -o '/[^ ]*nginx.conf' | head -1)
CONFDIR=$(dirname "$CONF")
echo "==> nginx: $NGINX  (config: $CONF)"

nginx_reload() {
  "$NGINX" -t
  if systemctl is-active --quiet nginx 2>/dev/null; then systemctl reload nginx
  elif pgrep -x nginx >/dev/null; then "$NGINX" -s reload
  else "$NGINX"; fi
}
need_certbot() {
  command -v certbot >/dev/null || { apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot >/dev/null; }
}

# ---------- unde scriem configurația (director deja inclus în nginx.conf) ----------
TARGET=""
while read -r pat; do
  case "$pat" in /*) full="$pat" ;; *) full="$CONFDIR/$pat" ;; esac
  dir=$(dirname "$full"); base=$(basename "$full")
  [ -d "$dir" ] || continue
  if [ "$base" = "*.conf" ]; then TARGET="$dir/nts-radio.conf"; break; fi
  if [ "$base" = "*" ]; then TARGET="$dir/nts-radio"; break; fi
done < <(grep -E '^[[:space:]]*include[[:space:]]+[^;]*\*[^;]*;' "$CONF" | grep -vE 'mime|fastcgi|modules' |
         sed -E 's/^[[:space:]]*include[[:space:]]+([^;]+);.*/\1/')
if [ -z "$TARGET" ]; then
  mkdir -p "$CONFDIR/nts-radio.d"
  if ! grep -q "nts-radio.d" "$CONF"; then
    cp "$CONF" "$CONF.bak.$(date +%Y%m%d%H%M%S)"
    sed -i -E '0,/^[[:space:]]*http[[:space:]]*\{/s//&\n    include nts-radio.d\/*.conf;/' "$CONF"
    echo "==> Am adăugat 'include nts-radio.d/*.conf;' în blocul http din $CONF (backup făcut)"
  fi
  TARGET="$CONFDIR/nts-radio.d/nts-radio.conf"
fi
rm -f "$TARGET"      # rescriem de la zero (rulare repetată)
echo "==> Fișier config NtS-Radio: $TARGET"

# ---------- domeniul e deja folosit de alt site? ----------
OTHER=$("$NGINX" -T 2>/dev/null | awk '
  /^# configuration file /{ f=$4; sub(/:$/,"",f); next }
  { print f "\t" $0 }' | awk -F'\t' -v d="$DOMAIN" '
  $2 ~ /^[ \t]*server_name[ \t]/ { n=split($2,a,/[ \t;]+/); for(i=1;i<=n;i++) if(a[i]==d) print $1 }' | sort -u)

PROXY="
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Icy-MetaData \$http_icy_metadata;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 24h;
        proxy_send_timeout 24h;"

if [ -n "$OTHER" ]; then
  # ================= domeniu partajat: port HTTPS separat =================
  HTTPS_PORT="${HTTPS_PORT:-8443}"
  echo "==> $DOMAIN este deja folosit în: $(echo "$OTHER" | tr '\n' ' ')"
  echo "    Site-ul existent rămâne neatins; NtS-Radio va fi pe https://$DOMAIN:$HTTPS_PORT"
  CERT=""; KEY=""
  for f in $OTHER; do
    c=$(grep -E '^[[:space:]]*ssl_certificate[[:space:]]' "$f" 2>/dev/null | head -1 | awk '{print $2}' | tr -d ';')
    k=$(grep -E '^[[:space:]]*ssl_certificate_key[[:space:]]' "$f" 2>/dev/null | head -1 | awk '{print $2}' | tr -d ';')
    if [ -n "$c" ] && [ -f "$c" ] && [ -n "$k" ]; then CERT=$c; KEY=$k; break; fi
  done
  if [ -z "$CERT" ] && [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    CERT=/etc/letsencrypt/live/$DOMAIN/fullchain.pem; KEY=/etc/letsencrypt/live/$DOMAIN/privkey.pem
  fi
  if [ -z "$CERT" ]; then
    WR=$(for f in $OTHER; do grep -E '^[[:space:]]*root[[:space:]]' "$f" | head -1 | awk '{print $2}' | tr -d ';'; done | head -1)
    if [ -n "$WR" ] && [ -d "$WR" ]; then
      echo "==> Nu există certificat pentru $DOMAIN – îl cer prin webroot-ul site-ului existent ($WR)"
      need_certbot
      certbot certonly --webroot -w "$WR" -d "$DOMAIN" --non-interactive --agree-tos "${EM[@]}"
      CERT=/etc/letsencrypt/live/$DOMAIN/fullchain.pem; KEY=/etc/letsencrypt/live/$DOMAIN/privkey.pem
    else
      echo "EROARE: site-ul existent pentru $DOMAIN nu are certificat SSL și nici 'root' pentru validare."
      echo "        Emite întâi un certificat pentru $DOMAIN și rulează din nou scriptul."
      exit 1
    fi
  fi
  echo "==> Certificat: $CERT"
  cat > "$TARGET" <<EOF
# NtS-Radio – generat de https-setup.sh (port HTTPS separat; site-ul existent $DOMAIN nu e atins)
server {
    listen $HTTPS_PORT ssl;
    server_name $DOMAIN;

    ssl_certificate     $CERT;
    ssl_certificate_key $KEY;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 1024m;      # upload muzică AutoDJ

    location / {$PROXY
    }
}
EOF
  nginx_reload
  BASE="https://$DOMAIN:$HTTPS_PORT"
else
  # ================= domeniu dedicat: 80 -> 443 =================
  mkdir -p "$WEBROOT/.well-known/acme-challenge"
  if [ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    cat > "$TARGET" <<EOF
# NtS-Radio (temporar, până la emiterea certificatului)
server {
    listen 80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root $WEBROOT; }
    location / {$PROXY
    }
}
EOF
    nginx_reload
    need_certbot
    certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" --non-interactive --agree-tos "${EM[@]}"
  fi
  cat > "$TARGET" <<EOF
# NtS-Radio – generat de https-setup.sh
server {
    listen 80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root $WEBROOT; }
    location / { return 301 https://\$host\$request_uri; }
}

server {
    listen 443 ssl;
    server_name $DOMAIN;

    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 1024m;      # upload muzică AutoDJ

    location / {$PROXY
    }
}
EOF
  nginx_reload
  BASE="https://$DOMAIN"
fi

# ---------- reînnoire automată ----------
if [ -d /etc/letsencrypt ]; then
  mkdir -p /etc/letsencrypt/renewal-hooks/deploy
  cat > /etc/letsencrypt/renewal-hooks/deploy/nts-radio-nginx.sh <<EOF
#!/bin/sh
$NGINX -t && ( systemctl reload nginx 2>/dev/null || $NGINX -s reload )
EOF
  chmod +x /etc/letsencrypt/renewal-hooks/deploy/nts-radio-nginx.sh
  if command -v certbot >/dev/null && ! systemctl list-timers 2>/dev/null | grep -q certbot && [ ! -f /etc/cron.d/certbot ]; then
    echo "17 3 * * * root certbot renew -q" > /etc/cron.d/nts-radio-certbot
  fi
fi

# ---------- adresa publică în NtS-Radio ----------
if [ -f "$DATA/nts-radio.db" ]; then
  python3 - "$DATA/nts-radio.db" "$BASE" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("INSERT INTO settings(key,value) VALUES('public_url',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
          (sys.argv[2],))
c.commit()
PYEOF
fi

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  [ -n "$HTTPS_PORT" ] && ufw allow "$HTTPS_PORT/tcp" >/dev/null
fi

sleep 1
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/login" || true)
echo
echo "================================================================"
if [ "$CODE" = "200" ]; then echo " HTTPS funcționează."
else echo " ATENȚIE: testul $BASE/login a întors '$CODE' – verifică firewall-ul (80/443${HTTPS_PORT:+/$HTTPS_PORT})."; fi
echo
echo " Admin:     $BASE/admin"
echo " Manager:   $BASE/manager"
echo " Player:    $BASE/s/radio"
echo " Stream:    $BASE/radio"
echo
echo " Studioul se conectează în continuare la: $DOMAIN port $PORT (Icecast) / $((PORT+1)) (SHOUTcast)"
echo " Certificatul se reînnoiește automat."
echo "================================================================"
