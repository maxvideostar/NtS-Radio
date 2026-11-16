#!/usr/bin/env bash
# =====================================================================
#  NtS-Radio – HTTPS prin nginx + certificat Let's Encrypt (automat)
#
#  Utilizare:
#    cd /opt/nts-radio && sudo bash https-setup.sh radio.domeniu.ro [email@domeniu.ro]
#
#  - merge cu nginx din apt SAU compilat din sursă (ex. cu nginx-rtmp-module)
#  - nu atinge celelalte site-uri / blocul rtmp (face backup la nginx.conf dacă îl modifică)
#  - reînnoire automată a certificatului + reload nginx
#  - studioul se conectează în continuare pe porturile NtS-Radio (8000 / 8001)
# =====================================================================
set -e
DOMAIN="$1"
EMAIL="$2"
[ "$(id -u)" -eq 0 ] || { echo "Rulează cu sudo."; exit 1; }
[ -n "$DOMAIN" ] || { echo "Utilizare: sudo bash https-setup.sh radio.domeniu.ro [email]"; exit 1; }

ENV_FILE=/etc/nts-radio.env
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
PORT="${NTS_PORT:-8000}"
DATA="${NTS_DATA:-/var/lib/nts-radio}"
WEBROOT=/var/www/letsencrypt

echo "==> Domeniu: $DOMAIN  ->  NtS-Radio pe 127.0.0.1:$PORT"

# ---------- verificare DNS ----------
MYIP=$(curl -4 -fs --max-time 5 https://api.ipify.org || true)
DNSIP=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')
if [ -z "$DNSIP" ]; then
  echo "EROARE: $DOMAIN nu are înregistrare DNS (A). Pune A record către ${MYIP:-IP-ul serverului} și reîncearcă."
  exit 1
fi
if [ -n "$MYIP" ] && [ "$DNSIP" != "$MYIP" ]; then
  echo "ATENȚIE: $DOMAIN -> $DNSIP, dar serverul are IP public $MYIP. Continui (poate e în spatele NAT)..."
fi

# ---------- nginx ----------
NGINX=$(command -v nginx || true)
[ -z "$NGINX" ] && [ -x /usr/local/nginx/sbin/nginx ] && NGINX=/usr/local/nginx/sbin/nginx
if [ -z "$NGINX" ]; then
  echo "==> nginx nu e instalat – îl instalez din apt"
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx >/dev/null
  NGINX=$(command -v nginx)
fi
"$NGINX" -V 2>&1 | grep -q "http_ssl_module" || { echo "EROARE: nginx-ul tău nu are modulul SSL (--with-http_ssl_module)."; exit 1; }
CONF=$("$NGINX" -V 2>&1 | grep -o -- '--conf-path=[^ ]*' | cut -d= -f2)
[ -z "$CONF" ] && CONF=/etc/nginx/nginx.conf
[ -f "$CONF" ] || CONF=$("$NGINX" -t 2>&1 | grep -o '/[^ ]*nginx.conf' | head -1)
CONFDIR=$(dirname "$CONF")
echo "==> nginx: $NGINX  (config: $CONF)"

nginx_reload() {
  "$NGINX" -t
  if systemctl is-active --quiet nginx 2>/dev/null; then systemctl reload nginx
  elif pgrep -x nginx >/dev/null; then "$NGINX" -s reload
  else "$NGINX"; fi
}

# unde punem fișierul nostru: într-un director deja inclus în nginx.conf
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
echo "==> Fișier config: $TARGET"
mkdir -p "$WEBROOT/.well-known/acme-challenge"

PROXY_BLOCK="
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

# ---------- pas 1: HTTP (pentru validarea Let's Encrypt) ----------
CERT=/etc/letsencrypt/live/$DOMAIN/fullchain.pem
if [ ! -f "$CERT" ]; then
  cat > "$TARGET" <<EOF
# NtS-Radio (temporar, până la emiterea certificatului)
server {
    listen 80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root $WEBROOT; }
    location / {$PROXY_BLOCK
    }
}
EOF
  nginx_reload
  echo "==> Certbot"
  command -v certbot >/dev/null || { apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot >/dev/null; }
  if [ -n "$EMAIL" ]; then EM=(-m "$EMAIL"); else EM=(--register-unsafely-without-email); fi
  certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" --non-interactive --agree-tos "${EM[@]}"
fi

# ---------- pas 2: HTTPS ----------
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
    ssl_session_cache shared:ntsradio_ssl:10m;
    ssl_session_timeout 1d;

    client_max_body_size 1024m;      # upload muzică AutoDJ

    location / {$PROXY_BLOCK
    }
}
EOF
nginx_reload

# reload nginx după fiecare reînnoire
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/nts-radio-nginx.sh <<EOF
#!/bin/sh
$NGINX -t && ( systemctl reload nginx 2>/dev/null || $NGINX -s reload )
EOF
chmod +x /etc/letsencrypt/renewal-hooks/deploy/nts-radio-nginx.sh
if ! systemctl list-timers 2>/dev/null | grep -q certbot && ! [ -f /etc/cron.d/certbot ]; then
  echo "17 3 * * * root certbot renew -q" > /etc/cron.d/nts-radio-certbot
fi

# adresa publică în NtS-Radio (linkuri player / m3u / embed cu https)
if [ -f "$DATA/nts-radio.db" ]; then
  python3 - "$DATA/nts-radio.db" "https://$DOMAIN" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("INSERT INTO settings(key,value) VALUES('public_url',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (sys.argv[2],))
c.commit()
EOF
fi

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi

sleep 1
CODE=$(curl -s -o /dev/null -w "%{http_code}" "https://$DOMAIN/login" || true)
echo
echo "================================================================"
[ "$CODE" = "200" ] && echo " HTTPS funcționează." || echo " ATENȚIE: testul https://$DOMAIN/login a întors '$CODE' – verifică porturile 80/443."
echo
echo " Admin:     https://$DOMAIN/admin"
echo " Manager:   https://$DOMAIN/manager"
echo " Player:    https://$DOMAIN/s/radio"
echo " Stream:    https://$DOMAIN/radio"
echo
echo " Studioul se conectează în continuare la: $DOMAIN port $PORT (Icecast) / $((PORT+1)) (SHOUTcast)"
echo " Certificatul se reînnoiește automat."
echo "================================================================"
