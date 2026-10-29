#!/usr/bin/env python3
"""Install persistent startup settings without interrupting the running USB test."""

from datetime import datetime
from pathlib import Path
import os
import platform
import shutil
import sys

# Validate permissions, platform, and the existing installation.
check_only = "--check" in sys.argv
if not check_only and os.geteuid() != 0:
    raise SystemExit("Run with sudo python3 ~/radionav-usb/finalize-autostart.py")
if platform.machine() != "armv6l":
    raise SystemExit("Expected the original Zero W (ARMv6)")
source = Path(__file__).resolve().parent
app = Path("/opt/radionav-usb")
if "// Cache sequential reads" not in (app / "upstream/stream/fs.go").read_text():
    raise SystemExit(
        "Optimized source missing; update/build the streaming program first"
    )
required = ["streamfs", "audio_bridge.py", "wait_ready.py", "silence.mp3"]
if not all((app / name).is_file() for name in required):
    raise SystemExit("Streaming installation is incomplete")
cmdpath = Path("/boot/firmware/cmdline.txt")
configpath = Path("/boot/firmware/config.txt")
cmd = cmdpath.read_text()
if len(cmd.splitlines()) != 1 or "root=PARTUUID=" not in cmd:
    raise SystemExit("Unexpected kernel command line")
# Remove only the USB diagnostic arguments introduced during setup.
tokens = []
units = [
    "radionav-usb.target",
    "radionav-streamfs.service",
    "radionav-audio.service",
    "radionav-gadget.service",
]
for token in cmd.split():
    if token in ["systemd.mask=" + unit for unit in units]:
        continue
    if token == "systemd.wants=serial-getty@ttyGS0.service":
        continue
    if token.startswith("modules-load="):
        modules = [
            module
            for module in token.split("=", 1)[1].split(",")
            if module != "g_serial"
        ]
        if "g_ether" in modules:
            raise SystemExit(
                "Another USB gadget is configured; resolve the conflict first"
            )
        if modules:
            tokens.append("modules-load=" + ",".join(modules))
    else:
        tokens.append(token)
# Prepare the permanent peripheral overlay and service files.
config = configpath.read_text()
marker = "# RadioNav autonomous USB peripheral"
if marker not in config:
    config = (
        config.rstrip()
        + "\n\n[all]\n"
        + marker
        + "\ndtoverlay=dwc2,dr_mode=peripheral\n"
    )
planned = {
    cmdpath: (" ".join(tokens) + "\n").encode(),
    configpath: config.encode(),
    app / "bluetooth_ready.py": (source / "bluetooth_ready.py").read_bytes(),
}
for name in units + ["radionav-bluetooth.service"]:
    planned[Path("/etc/systemd/system") / name] = (source / name).read_bytes()
# Order the audio bridge after Bluetooth preparation.
audio = Path("/etc/systemd/system/radionav-audio.service")
text = planned[audio].decode()
text = text.replace(
    "Requires=radionav-streamfs.service bluealsa.service",
    "Requires=radionav-streamfs.service bluealsa.service radionav-bluetooth.service",
)
text = text.replace(
    "After=radionav-streamfs.service bluealsa.service bluetooth.service",
    "After=radionav-streamfs.service bluealsa.service bluetooth.service radionav-bluetooth.service",
)
planned[audio] = text.encode()
link = Path("/etc/systemd/system/multi-user.target.wants/radionav-usb.target")
if link.is_symlink() and link.resolve() != Path(
    "/etc/systemd/system/radionav-usb.target"
):
    raise SystemExit("Unexpected existing target link")
if link.exists() and not link.is_symlink():
    raise SystemExit("Unexpected regular file at target link")
# Report the planned changes without modifying the system.
if check_only:
    print("Preflight OK; will update:", *[str(path) for path in planned], sep="\n")
    print("Proposed cmdline:", " ".join(tokens))
    raise SystemExit(0)
# Back up existing files before applying persistent configuration.
stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = Path("/var/lib/radionav-usb/backups") / stamp
backup.mkdir(parents=True)
originals = {path: path.read_bytes() if path.exists() else None for path in planned}
for path, data in originals.items():
    if data is not None:
        destination = backup / path.relative_to("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
# Write configuration and restore saved files if any operation fails.
try:
    for path, data in planned.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        assert path.read_bytes() == data
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.is_symlink():
        link.symlink_to("/etc/systemd/system/radionav-usb.target")
except BaseException:
    for path, data in originals.items():
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(data)
    raise
print("Autostart prepared. Backups:", backup)
print("Close the PC audio player and eject the virtual disk before rebooting.")
print("Reboot using: sudo reboot")
print("First pairing remains a manual step; this phone is already paired and trusted.")
