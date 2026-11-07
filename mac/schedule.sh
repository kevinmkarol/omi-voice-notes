#!/bin/zsh
# ABOUTME: Keeps the pendant synced indefinitely: installs a LaunchAgent that runs omi-sync every INTERVAL (default 10m) and
# ABOUTME: pulls, transcribes and frees the pendant's storage whenever it is in range. `schedule.sh remove` takes it out again.
set -euo pipefail
here=${0:A:h}
me=$0
label=omi-voice-notes.sync
agent=~/Library/LaunchAgents/$label.plist

usage() {
  echo "usage: $me [INTERVAL | remove]    INTERVAL like 90s, 10m or 1h (default 10m); at least 30s" >&2
  exit 2
}

if [[ ${1:-} == remove ]]; then
  launchctl bootout gui/$UID/$label 2>/dev/null || true
  rm -f $agent
  echo "removed $label"
  exit 0
fi

interval=${1:-10m}
case $interval in
  <->s) seconds=${interval%s} ;;
  <->m) seconds=$(( ${interval%m} * 60 )) ;;
  <->h) seconds=$(( ${interval%h} * 3600 )) ;;
  <->)  seconds=$interval ;;
  *)    usage ;;
esac
(( seconds >= 30 )) || usage

command -v uv >/dev/null || { echo "missing uv: run setup.sh first"; exit 1; }
command -v whisper-cli >/dev/null || { echo "missing whisper-cli: run setup.sh first"; exit 1; }
mkdir -p $here/logs
(cd $here && uv sync -q --no-dev)

# OMI_NOTES_* variables set now are written into the agent, since launchd gives it no environment of its own.
extra=""
baked=""
for var in OMI_NOTES_ARCHIVE OMI_NOTES_WEBHOOK OMI_NOTES_WEBHOOK_TOKEN OMI_NOTES_MODELS OMI_NOTES_VOCABULARY; do
  value=${(P)var:-}
  [[ -n $value ]] || continue
  baked+=" $var"
  value=${value//&/&amp;}; value=${value//</&lt;}
  extra+="<key>$var</key><string>$value</string>"
done
extra=${extra//&/\\&}
sed -e "s|__MAC_DIR__|$here|g" -e "s|__UV__|$(command -v uv)|g" -e "s|__INTERVAL__|$seconds|g" -e "s|__EXTRA_ENV__|$extra|" $here/$label.plist > $agent
chmod 600 $agent   # it may hold the webhook token
plutil -lint -s $agent
launchctl bootout gui/$UID/$label 2>/dev/null || true
for _ in {1..20}; do launchctl print gui/$UID/$label >/dev/null 2>&1 || break; sleep 0.5; done
launchctl bootstrap gui/$UID $agent

echo "$label runs every $interval (first run now); log: $here/logs/omi-sync.log"
echo "First run only: macOS asks whether Python may use Bluetooth. Click Allow, or every run exits with a Bluetooth error."
[[ -n $baked ]] && echo "baked in:$baked" || echo "To send notes somewhere, run this with OMI_NOTES_WEBHOOK set (see docs/INTEGRATIONS.md)."
