#!/usr/bin/env bash
# =====================================================================
#  NtS-Radio – instalare / actualizare (Ubuntu 22.04 / 24.04, Debian 12)
#
#  Instalare:
#    git clone https://github.com/maxvideostar/NtS-Radio.git /opt/nts-radio
#    cd /opt/nts-radio && sudo bash install.sh
#
#  Actualizare (aceeași comandă, păstrează datele și setările):
#    cd /opt/nts-radio && sudo git pull && sudo bash install.sh
#
#  Opțional: alt port (implicit 8000; sursa SHOUTcast folosește port+1)
#    sudo NTS_PORT=8010 bash install.sh
# =====================================================================
set -e
[ "$(id -u)" -eq 0 ] || { echo "Rulează cu sudo: sudo bash install.sh"; exit 1; }

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR=/var/lib/nts-radio
ENV_FILE=/etc/nts-radio.env
SVC=/etc/systemd/system/nts-radio.service
USER_NAME=ntsradio

echo "==> NtS-Radio din $APP_DIR"

echo "==> Pachete (python3, ffmpeg, git, tzdata)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 ffmpeg git curl tzdata ca-certificates >/dev/null

python3 - <<'EOF' || { echo "EROARE: e nevoie de Python 3.9 sau mai nou (Ubuntu 22.04+)."; exit 1; }
import sys, zoneinfo
assert sys.version_info >= (3, 9)
EOF

echo "==> Utilizator de sistem și director de date"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$USER_NAME"
mkdir -p "$DATA_DIR"
chown -R "$USER_NAME:$USER_NAME" "$DATA_DIR"
chmod 750 "$DATA_DIR"

if [ ! -f "$ENV_FILE" ]; then
  PORT="${NTS_PORT:-8000}"
  cat > "$ENV_FILE" <<EOF
# NtS-Radio – configurare (după modificare: systemctl restart nts-radio)
# Portul principal: ascultători, panou web și surse Icecast.
# Sursele SHOUTcast v1 folosesc automat portul următor (NTS_PORT+1).
NTS_PORT=$PORT
NTS_HOST=0.0.0.0
NTS_DATA=$DATA_DIR
EOF
  echo "==> Configurare creată: $ENV_FILE (port $PORT)"
elif [ -n "$NTS_PORT" ]; then
  sed -i "s/^NTS_PORT=.*/NTS_PORT=$NTS_PORT/" "$ENV_FILE"
fi
# shellcheck disable=SC1090
. "$ENV_FILE"

echo "==> Serviciu systemd"
cat > "$SVC" <<EOF
[Unit]
Description=NtS-Radio streaming server
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
Group=$USER_NAME
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/nts-radio.py
Restart=always
RestartSec=2
LimitNOFILE=65536
Nice=-5
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF
chmod -R a+rX "$APP_DIR"
systemctl daemon-reload
systemctl enable nts-radio >/dev/null 2>&1
systemctl restart nts-radio

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  echo "==> Deschid porturile $NTS_PORT și $((NTS_PORT+1)) în ufw"
  ufw allow "$NTS_PORT/tcp" >/dev/null
  ufw allow "$((NTS_PORT+1))/tcp" >/dev/null
fi

echo "==> Aștept pornirea..."
for i in $(seq 1 20); do
  sleep 1
  if curl -fs -o /dev/null "http://127.0.0.1:$NTS_PORT/login"; then break; fi
done

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "================================================================"
if systemctl is-active --quiet nts-radio; then
  echo " NtS-Radio rulează."
else
  echo " ATENȚIE: serviciul nu a pornit. Vezi: journalctl -u nts-radio -n 50"
fi
echo
echo " Panou admin:        http://$IP:$NTS_PORT/admin"
echo " Pagina managerului: http://$IP:$NTS_PORT/manager"
echo " Player public:      http://$IP:$NTS_PORT/s/radio"
echo " Stream:             http://$IP:$NTS_PORT/radio"
if [ -f "$DATA_DIR/admin-password.txt" ]; then
  echo
  echo " Cont admin inițial (schimbă parola din Setări):"
  sed 's/^/   /' "$DATA_DIR/admin-password.txt"
  echo "   (fișier: $DATA_DIR/admin-password.txt – șterge-l după ce schimbi parola)"
fi
echo
echo " Porturi de deschis în firewall: $NTS_PORT/tcp și $((NTS_PORT+1))/tcp"
echo " Jurnal: journalctl -u nts-radio -f"
echo "================================================================"
