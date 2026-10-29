#!/usr/bin/env python3
"""Power the adapter for an already-paired phone before starting audio capture."""

import pathlib
import shutil
import subprocess
import time

# Unblock Bluetooth before powering the existing adapter.
rfkill = shutil.which("rfkill") or "/usr/sbin/rfkill"
subprocess.run([rfkill, "unblock", "bluetooth"], check=True, timeout=5)
base = ["busctl", "set-property", "org.bluez", "/org/bluez/hci0", "org.bluez.Adapter1"]
# Wait for BlueZ to expose and power the controller.
for attempt in range(15):
    result = subprocess.run(
        base + ["Powered", "b", "true"], capture_output=True, text=True, timeout=5
    )
    if result.returncode == 0:
        break
    time.sleep(1)
else:
    raise SystemExit("Bluetooth adapter did not become ready: " + result.stderr)
# Set the display name while keeping automatic reconnection private.
for args in [
    ["Alias", "s", "Giulietta-USB"],
    ["DiscoverableTimeout", "u", "0"],
    ["Discoverable", "b", "false"],
]:
    subprocess.run(base + args, check=True, timeout=5)
print("Bluetooth ready. Pairing records preserved; audio bridge handles reconnection.")
