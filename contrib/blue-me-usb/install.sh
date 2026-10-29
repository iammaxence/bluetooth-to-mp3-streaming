#!/bin/bash
# Install the current Bluetooth/USB engine from this local checkout.
set -euo pipefail

# Require root and the platform used for the validated streaming setup.
if [[ $EUID -ne 0 ]]; then
    echo "Run: sudo bash $0" >&2
    exit 1
fi
. /etc/os-release
if [[ $ID != raspbian || $VERSION_ID != 13 || $(uname -m) != armv6l ]]; then
    echo "Expected an original Zero W, Raspbian 13, 32 bits." >&2
    exit 1
fi

# Resolve the checkout; never clone, download, or patch another repository.
SOURCE_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(cd -- "$SOURCE_DIR/../.." && pwd)"
for path in go.mod cmd/streamfs.go files/demo3/imagem.raw.tar.gz; do
    if [[ ! -f "$REPO_ROOT/$path" ]]; then
        echo "Incomplete local checkout: $path" >&2
        exit 1
    fi
done
for path in audio_bridge.py engine.py bluetooth_ready.py wait_ready.py test_engine.py; do
    if [[ ! -f "$SOURCE_DIR/$path" ]]; then
        echo "Missing integration file: $path" >&2
        exit 1
    fi
done
APP=/opt/radionav-usb
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/var/lib/radionav-usb/backups/install-$STAMP"

# Log failures and retain the previous installed files for manual recovery.
install -d -m 755 /var/log/radionav-usb "$APP" "$BACKUP"
exec > >(tee -a /var/log/radionav-usb/install.log) 2>&1
trap 'echo "Stopped at line $LINENO; log: /var/log/radionav-usb/install.log; backups: $BACKUP"' ERR

# Save one existing managed file under its original relative path.
backup_file() {
    local path="$1"
    if [[ -f "$path" ]]; then
        local destination="$BACKUP/${path#/}"
        install -d -m 755 "$(dirname -- "$destination")"
        cp -p -- "$path" "$destination"
    fi
}

# Remove only this installer's temporary build directory when it exits.
cleanup() {
    if [[ -n ${STAGING:-} && -d "$STAGING" ]]; then
        rm -rf -- "$STAGING"
    fi
}

# Install dependencies; Internet is used only for packages and Go modules.
echo "Installing dependencies..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 golang-go lame bluez bluez-alsa-utils libasound2-plugin-bluez \
    alsa-utils fuse3 rfkill kmod
STAGING="$(mktemp -d /var/tmp/radionav-install.XXXXXX)"
trap cleanup EXIT

# Validate the active PCM engine and compile the sources from this checkout.
python3 -B -m unittest discover -s "$SOURCE_DIR" -p test_engine.py -v
echo "Building local sources for ARMv6..."
cd "$REPO_ROOT"
env GOOS=linux GOARCH=arm GOARM=6 CGO_ENABLED=0 GOMAXPROCS=1 \
    GOMEMLIMIT=160MiB GOTOOLCHAIN=local \
    go build -p 1 -o "$STAGING/streamfs" cmd/streamfs.go

# Use independent MP3 frames in both idle silence and the active audio bridge.
echo "Generating startup silence..."
python3 -c 'import sys; sys.stdout.buffer.write(bytes(44100 * 4 * 11))' |
    lame --silent --nores --noreplaygain -r -s 44.1 --bitwidth 16 \
        --signed --little-endian -m s -b 128 -q 7 - "$STAGING/silence.mp3"
python3 - "$STAGING/silence.mp3" <<'PY'
from pathlib import Path
import sys
assert Path(sys.argv[1]).stat().st_size > 128 * 1024
PY

# Prepare the disk from the bundled archive without modifying local sources.
install -d -m 755 "$APP/upstream/files/demo3"
if [[ ! -f "$APP/upstream/files/demo3/imagem.raw" ]]; then
    tar -xzf "$REPO_ROOT/files/demo3/imagem.raw.tar.gz" -C "$APP/upstream/files/demo3"
fi

# Stop existing units explicitly before replacing executables or releasing FUSE.
UNITS=(radionav-streamfs.service radionav-audio.service radionav-gadget.service
       radionav-bluetooth.service radionav-usb.target)
EXISTING=()
for name in "${UNITS[@]}"; do
    if systemctl cat "$name" >/dev/null 2>&1; then
        EXISTING+=("$name")
    fi
done
if (( ${#EXISTING[@]} )); then
    systemctl stop "${EXISTING[@]}"
fi
if awk '$5 == "/run/radionav-usb" { found=1 } END { exit !found }' /proc/self/mountinfo; then
    fusermount3 -uz /run/radionav-usb
fi

# Install the merged runtime; preserve any previously installed version.
for name in streamfs silence.mp3 audio_bridge.py engine.py bluetooth_ready.py wait_ready.py; do
    backup_file "$APP/$name"
    if [[ $name == streamfs || $name == silence.mp3 ]]; then
        source="$STAGING/$name"
    else
        source="$SOURCE_DIR/$name"
    fi
    mode=644
    if [[ $name == streamfs ]]; then
        mode=755
    fi
    install -m "$mode" "$source" "$APP/.$name.installing"
    mv -f -- "$APP/.$name.installing" "$APP/$name"
done

# Configure BlueALSA to receive Bluetooth A2DP audio.
DROPIN=/etc/systemd/system/bluealsa.service.d/radionav-sink.conf
backup_file "$DROPIN"
install -d -m 755 "$(dirname -- "$DROPIN")"
cat > "$DROPIN" <<'UNIT'
[Service]
ExecStart=
ExecStart=/usr/bin/bluealsa -p a2dp-sink
UNIT
if systemctl cat bluealsa-aplay.service >/dev/null 2>&1; then
    systemctl disable --now bluealsa-aplay.service
fi

# Install the permanent units, including Bluetooth readiness and PCM status storage.
for name in "${UNITS[@]}"; do
    backup_file "/etc/systemd/system/$name"
    install -m 644 "$SOURCE_DIR/$name" "/etc/systemd/system/$name"
done

# Supersede the old migration override: the main service now runs this version.
OLD_OVERRIDE=/etc/systemd/system/radionav-audio.service.d/adaptive.conf
if [[ -f "$OLD_OVERRIDE" ]]; then
    backup_file "$OLD_OVERRIDE"
    rm -f -- "$OLD_OVERRIDE"
fi

# Require an effective overlay under [all], rather than matching a CM4/CM5 section.
BOOT_CONFIG=/boot/firmware/config.txt
backup_file "$BOOT_CONFIG"
python3 - "$BOOT_CONFIG" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text()
section = ""
configured = False
for line in text.splitlines():
    line = line.strip()
    if line.startswith("[") and line.endswith("]"):
        section = line[1:-1]
    if section in ("", "all") and line == "dtoverlay=dwc2,dr_mode=peripheral":
        configured = True
if not configured:
    path.write_text(text.rstrip() + "\n\n[all]\n# RadioNav USB streaming\n"
                    "dtoverlay=dwc2,dr_mode=peripheral\n")
PY

# Load the peripheral controller at boot without changing networking or SSH.
backup_file /etc/modules-load.d/radionav-usb.conf
install -d -m 755 /etc/modules-load.d
printf 'dwc2\n' > /etc/modules-load.d/radionav-usb.conf

# Enable the installed version; the reboot activates USB boot configuration.
systemctl daemon-reload
systemd-analyze verify "${UNITS[@]/#//etc/systemd/system/}"
systemctl enable --now bluetooth.service
systemctl enable bluealsa.service radionav-usb.target
systemctl restart bluealsa.service

echo "Installation complete. Previous files: $BACKUP"
echo "Reboot using: sudo reboot"
echo "First Bluetooth pairing: see contrib/blue-me-usb/README.md."
echo "Disable streaming: sudo systemctl disable --now radionav-usb.target"
