#!/usr/bin/env python3
"""Adaptive Bluetooth/PCM/MP3 runtime. Each generation owns its encoder and TCP session."""

import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading
import time
from engine import Engine

STOP = threading.Event()
ENGINE = Engine()
PHONE = None
PHONE_LOCK = threading.Lock()


# Execute a bounded control request without a shell.
def command(args, timeout=5):
    """Execute a bounded control request without a shell."""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


# Unwrap BlueZ busctl JSON variants.
def value(item, default=None):
    """Unwrap BlueZ busctl JSON variants."""
    return item.get("data", default) if isinstance(item, dict) else default


# Read AVRCP track/status; retain state across short D-Bus failures.
def source_monitor():
    """Read AVRCP track/status; retain state across short D-Bus failures."""
    global PHONE
    failures = 0
    reconnect_at = 0
    retry = 1
    while not STOP.is_set():
        try:
            result = command(
                [
                    "busctl",
                    "--json=short",
                    "call",
                    "org.bluez",
                    "/",
                    "org.freedesktop.DBus.ObjectManager",
                    "GetManagedObjects",
                ]
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip())
            objects = json.loads(result.stdout)["data"][0]
            trusted = []
            for path, interfaces in objects.items():
                device = interfaces.get("org.bluez.Device1", {})
                if value(device.get("Trusted"), False):
                    trusted.append((path, device))
            trusted.sort()
            if not trusted:
                ENGINE.transition(False, "stopped", None)
                STOP.wait(1)
                continue
            path, device = trusted[0]
            address = value(device.get("Address"))
            with PHONE_LOCK:
                PHONE = address
            connected = value(device.get("Connected"), False)
            if not connected and time.monotonic() >= reconnect_at:
                command(
                    ["bluetoothctl", "--timeout", "3", "connect", address], timeout=5
                )
                reconnect_at = time.monotonic() + retry
                retry = min(30, retry * 2)
            if connected:
                retry = 1
            control = objects[path].get("org.bluez.MediaControl1", {})
            player_path = value(control.get("Player"))
            player = objects.get(player_path, {}).get("org.bluez.MediaPlayer1", {})
            track_data = value(player.get("Track"), {})
            track = {
                k: value(track_data[k])
                for k in ("Title", "Artist", "Album", "TrackNumber", "Duration")
                if k in track_data
            }
            status = value(player.get("Status"), "playing" if connected else "stopped")
            ENGINE.transition(connected, status, track or None)
            failures = 0
        except (
            OSError,
            ValueError,
            KeyError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as error:
            failures += 1
            if failures == 3:
                ENGINE.transition(False, "stopped", None)
                print("Source metadata unavailable:", str(error)[:200], flush=True)
        STOP.wait(0.5)


# Bound subprocess shutdown, including a nonresponsive ALSA/encoder child.
def stop_process(process):
    """Bound subprocess shutdown, including a nonresponsive ALSA/encoder child."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


# Restart ALSA capture on generation changes, rejecting old in-flight PCM.
def capture():
    """Restart ALSA capture on generation changes, rejecting old in-flight PCM."""
    while not STOP.is_set():
        state = ENGINE.snapshot()
        if not state["connected"] or state["source_status"] != "playing":
            STOP.wait(0.1)
            continue
        with PHONE_LOCK:
            address = PHONE
        if not address:
            STOP.wait(0.1)
            continue
        generation = state["generation"]
        process = subprocess.Popen(
            [
                "arecord",
                "-q",
                "-D",
                "bluealsa:SRV=org.bluealsa,DEV=" + address + ",PROFILE=a2dp",
                "-t",
                "raw",
                "-f",
                "S16_LE",
                "-r",
                "44100",
                "-c",
                "2",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            while not STOP.is_set():
                # select bounds reads, so a paused device cannot trap this thread.
                import select

                ready, _, _ = select.select([process.stdout], [], [], 0.1)
                if ENGINE.snapshot()["generation"] != generation:
                    break
                if not ready:
                    if process.poll() is not None:
                        break
                    continue
                data = os.read(process.stdout.fileno(), 8192)
                if not data:
                    break
                ENGINE.ingest(data, time.monotonic(), generation)
        finally:
            stop_process(process)
            process.stdout.close()
            ENGINE.capture_lost(generation)
        STOP.wait(0.1)


class EncoderSession:
    """Keep the encoder reservoir and the server queue scoped to one generation."""

    # Connect the producer socket and start the LAME encoder and forwarding thread.
    def __init__(self):
        self.stop = threading.Event()
        self.socket = socket.create_connection(
            ("127.0.0.1", int(os.environ.get("RADIONAV_AUDIO_PORT", "5000"))), timeout=2
        )
        self.socket.settimeout(2)
        self.process = subprocess.Popen(
            [
                "lame",
                "--silent",
                "--nores",
                "--noreplaygain",
                "-r",
                "-s",
                "44.1",
                "--bitwidth",
                "16",
                "--signed",
                "--little-endian",
                "-m",
                "s",
                "-b",
                "128",
                "-q",
                "7",
                "-",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.thread = threading.Thread(target=self.forward, daemon=True)
        self.thread.start()

    # Cancel the encoder if the USB producer socket stops accepting data.
    def forward(self):
        """Cancel the encoder if the USB producer socket stops accepting data."""
        try:
            while not STOP.is_set() and not self.stop.is_set():
                data = os.read(self.process.stdout.fileno(), 8192)
                if not data:
                    break
                self.socket.sendall(data)
        except OSError:
            pass
        finally:
            self.stop.set()
            if self.process.poll() is None:
                self.process.terminate()

    # Close the old TCP stream before creating its replacement.
    def close(self):
        """Close the old TCP stream before creating its replacement."""
        self.stop.set()
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()
        stop_process(self.process)
        self.thread.join(timeout=3)
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except OSError:
                pass


# Render steady 20 ms blocks and expose state without storing audio.
def main():
    """Render steady 20 ms blocks and expose state without storing audio."""
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    threading.Thread(target=source_monitor, daemon=True).start()
    threading.Thread(target=capture, daemon=True).start()
    session = None
    generation = -1
    deadline = time.monotonic()
    report_at = 0
    try:
        while not STOP.is_set():
            state = ENGINE.snapshot()
            if (
                session is None
                or state["generation"] != generation
                or session.stop.is_set()
            ):
                if session:
                    session.close()
                    session = None
                try:
                    session = EncoderSession()
                except OSError as error:
                    print("Encoder connection retry:", error, flush=True)
                    STOP.wait(1)
                    continue
                generation = state["generation"]
                deadline = time.monotonic()
            try:
                session.process.stdin.write(ENGINE.pull())
                session.process.stdin.flush()
            except (OSError, BrokenPipeError):
                session.close()
                session = None
                continue
            now = time.monotonic()
            if now >= report_at:
                destination = Path(
                    os.environ.get(
                        "RADIONAV_STATUS", "/run/radionav-audio/engine-status.json"
                    )
                )
                try:
                    temporary = destination.with_suffix(".tmp")
                    temporary.write_text(json.dumps(ENGINE.snapshot()) + "\n")
                    temporary.replace(destination)
                except OSError as error:
                    print("Status update:", error, flush=True)
                report_at = now + 1
            deadline += 0.02
            # Do not send a burst of old blocks after a scheduling stall.
            if now - deadline > 0.04:
                deadline = now
            STOP.wait(max(0, deadline - time.monotonic()))
    finally:
        if session:
            session.close()


if __name__ == "__main__":
    main()
