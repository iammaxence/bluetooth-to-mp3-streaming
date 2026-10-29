#!/usr/bin/env python3
"""Prefill valid MP3 before exposing the FUSE image as a USB disk."""

import os
from pathlib import Path
import socket
import time

# Wait for the FUSE filesystem before opening its producer socket.
deadline = time.monotonic() + 300
while not os.path.ismount("/run/radionav-usb"):
    if time.monotonic() >= deadline:
        raise SystemExit("FUSE mount did not appear")
    time.sleep(0.25)
# Load enough startup silence to initialize the streaming connection.
data = Path("/opt/radionav-usb/silence.mp3").read_bytes()
assert len(data) > 128 * 1024
# Send startup MP3 data before exposing the virtual disk over USB.
with socket.create_connection(("127.0.0.1", 5000), timeout=10) as connection:
    connection.sendall(data)
