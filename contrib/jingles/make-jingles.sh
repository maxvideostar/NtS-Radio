#!/usr/bin/env bash
# =====================================================================
#  NtS-Radio – generator de jingle-uri cu voce naturală (Microsoft Edge neural TTS)
#
#  Utilizare (pe server, are nevoie de internet):
#    cd /opt/nts-radio && sudo bash contrib/jingles/make-jingles.sh "NetStreaming. Streaming services."
#
#  Opțiuni (variabile de mediu):
#    VOICE=en-US-ChristopherNeural   vocea (altele: en-US-GuyNeural, en-GB-RyanNeural,
#                                    ro-RO-EmilNeural, ro-RO-AlinaNeural)
#    RATE=+0%  PITCH=-2Hz            viteza / tonul vocii
#    SHORT="NetStreaming!"           textul scurt pentru sweeper (implicit: primul cuvânt)
#    STATION=radio                   copiază rezultatul direct în jingle-urile postului (slug)
#    VOICE_FILE=/cale/voce.mp3       folosește o înregistrare proprie în loc de TTS
#
#  Lista vocilor:  /var/lib/nts-radio/tts-venv/bin/edge-tts --list-voices | grep -E "en-|ro-"
# =====================================================================
set -e
TEXT="$1"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BEDS="$DIR/beds"
VOICE="${VOICE:-en-US-ChristopherNeural}"
RATE="${RATE:-+0%}"
PITCH="${PITCH:--2Hz}"
OUT="${OUT:-$PWD/jingles-$(date +%Y%m%d-%H%M%S)}"
VENV=/var/lib/nts-radio/tts-venv
[ -n "$TEXT" ] || [ -n "$VOICE_FILE" ] || { echo "Utilizare: bash make-jingles.sh \"Textul pentru voce\""; exit 1; }
command -v ffmpeg >/dev/null || { echo "Lipsește ffmpeg"; exit 1; }
mkdir -p "$OUT"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

if [ -n "$VOICE_FILE" ]; then
  cp "$VOICE_FILE" "$TMP/full.audio"
  cp "$VOICE_FILE" "$TMP/short.audio"
else
  if [ ! -x "$VENV/bin/edge-tts" ]; then
    echo "==> Instalez edge-tts (o singură dată)"
    rm -rf "$VENV"
    if ! python3 -m venv "$VENV" >/dev/null 2>&1; then
      rm -rf "$VENV"
      PYV=$(python3 -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
      echo "    instalez python3-venv..."
      apt-get install -y python3-venv "python${PYV}-venv" || apt-get install -y python3-venv
      python3 -m venv "$VENV"
    fi
    "$VENV/bin/pip" install -q --upgrade pip edge-tts
  fi
  SHORT="${SHORT:-$(echo "$TEXT" | awk '{print $1}' | sed 's/[.,;:]*$//')!}"
  echo "==> Voce: $VOICE"
  "$VENV/bin/edge-tts" --voice "$VOICE" --rate="$RATE" --pitch="$PITCH" --text "$TEXT"  --write-media "$TMP/full.audio"
  "$VENV/bin/edge-tts" --voice "$VOICE" --rate="$RATE" --pitch="$PITCH" --text "$SHORT" --write-media "$TMP/short.audio"
fi

dur() { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1"; }

# mix: <bed> <voce> <întârziere secunde> <ieșire> <LUFS>
mix() {
  local bed="$1" voc="$2" delay_ms len
  delay_ms=$(awk "BEGIN{printf \"%d\", $3*1000}")
  len=$(awk -v b="$(dur "$bed")" -v v="$(dur "$voc")" -v d="$3" 'BEGIN{l=d+v+0.8; if(b>l)l=b; printf "%.2f", l}')
  ffmpeg -y -loglevel error -i "$bed" -i "$voc" -filter_complex "
    [1:a]aresample=44100,aformat=channel_layouts=stereo,highpass=f=80,
         equalizer=f=3000:t=q:w=1.2:g=2,acompressor=threshold=0.12:ratio=3:attack=5:release=120:makeup=2,
         adelay=${delay_ms}|${delay_ms},apad,asplit=2[v][sc];
    [0:a]apad[b0];[b0][sc]sidechaincompress=threshold=0.02:ratio=8:attack=15:release=350[bg];
    [bg][v]amix=inputs=2:duration=longest:normalize=0,atrim=0:${len},afade=t=out:st=$(awk "BEGIN{print $len-0.4}"):d=0.4,loudnorm=I=$5:TP=-1:LRA=11" \
    -ar 44100 -c:a libmp3lame -b:a 320k -metadata artist="NtS-Radio" -metadata title="$(basename "$4" .mp3)" "$4"
  echo "   ✔ $4"
}

echo "==> Mixez jingle-urile"
mix "$BEDS/ident.mp3"   "$TMP/full.audio"  2.0  "$OUT/jingle-ident.mp3"   -11
mix "$BEDS/morse.mp3"   "$TMP/full.audio"  2.15 "$OUT/jingle-morse.mp3"   -11
mix "$BEDS/sweeper.mp3" "$TMP/short.audio" 0.95 "$OUT/jingle-sweeper.mp3" -11
# bed lung: textul complet la început + cel scurt pe final
ffmpeg -y -loglevel error -i "$TMP/full.audio" -i "$TMP/short.audio" -filter_complex \
  "[0:a]aresample=44100,aformat=channel_layouts=stereo[a];[1:a]aresample=44100,aformat=channel_layouts=stereo,adelay=10750|10750[b];[a][b]amix=inputs=2:duration=longest:normalize=0" \
  -c:a pcm_s16le "$TMP/both.wav"
mix "$BEDS/bed.mp3"     "$TMP/both.wav"    1.5  "$OUT/jingle-lung.mp3"    -12


if [ -n "$STATION" ]; then
  DEST=/var/lib/nts-radio/stations/$STATION/jingles
  if [ -d "/var/lib/nts-radio/stations/$STATION" ]; then
    mkdir -p "$DEST" && cp "$OUT"/*.mp3 "$DEST"/ && chown -R ntsradio:ntsradio "$DEST" 2>/dev/null || true
    echo "==> Copiate în jingle-urile postului '$STATION' ($DEST)"
  else
    echo "   ! Postul '$STATION' nu există în /var/lib/nts-radio/stations/"
  fi
fi
echo
echo "Gata: $OUT"
ls -1 "$OUT"
