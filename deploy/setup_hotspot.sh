#!/usr/bin/env bash
# Makes the Jetson create its own Wi-Fi network, also after every restart.
# Run once on the Jetson:   bash deploy/setup_hotspot.sh
# If 5 GHz does not work:   BAND=bg CHANNEL=6 bash deploy/setup_hotspot.sh
set -eo pipefail

SSID="${SSID:-haebotwifi}"
BAND="${BAND:-bg}"         # bg = 2.4 GHz (works without a Wi-Fi country set), a = 5 GHz (needs the country, e.g. sudo iw reg set KR)
CHANNEL="${CHANNEL:-6}"
IFACE="${IFACE:-$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2=="wifi"{print $1; exit}')}"

[ -n "$IFACE" ] || { echo "No Wi-Fi device found."; exit 1; }
read -rsp "Choose a Wi-Fi password for \"$SSID\" (8 or more characters): " PASS; echo
[ "${#PASS}" -ge 8 ] || { echo "Too short."; exit 1; }

sudo nmcli con delete kiro-ap >/dev/null 2>&1 || true
sudo nmcli con add type wifi ifname "$IFACE" con-name kiro-ap autoconnect yes ssid "$SSID" \
  802-11-wireless.mode ap 802-11-wireless.band "$BAND" 802-11-wireless.channel "$CHANNEL" \
  ipv4.method shared wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS"
sudo nmcli con modify kiro-ap connection.autoconnect-priority 100
sudo nmcli con up kiro-ap

echo
echo "Hotspot \"$SSID\" is on ($IFACE). Join it from the laptop, then open http://10.42.0.1:8080"
