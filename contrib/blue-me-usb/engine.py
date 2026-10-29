"""Deterministic adaptive PCM playout, independent of Bluetooth and subprocesses."""

import array
import math
import threading


class Engine:
    """Bound PCM age and adapt prebuffering from arrival jitter and underruns."""

    rate = 44100
    frame_bytes = 4
    block_frames = 882  # 20 ms

    # Initialize playback state, PCM storage, and adaptive buffer metrics.
    def __init__(self):
        self.lock = threading.RLock()
        self.buffer = bytearray()
        self.tail = b""
        self.state = "disconnected"
        self.generation = 0
        self.track = None
        self.connected = False
        self.status = "stopped"
        self.target_ms = 160.0
        self.jitter_ms = 0.0
        self.last_arrival = None
        self.previous_duration = 0.0
        self.last_sample = (0, 0)
        self.fade = True
        self.underruns = 0
        self.dropped_frames = 0
        self.transitions = 0
        self.capture_available = False

    # Start a fresh generation only on real transport/status/track changes.
    def transition(self, connected, status, track):
        """Start a fresh generation only on real transport/status/track changes."""
        with self.lock:
            if status not in ("playing", "paused", "stopped"):
                status = "playing" if connected else "stopped"
            changed = (connected, status, track) != (
                self.connected,
                self.status,
                self.track,
            )
            if not changed:
                return False
            self.connected, self.status, self.track = connected, status, track
            self.generation += 1
            self.transitions += 1
            self.buffer.clear()
            self.tail = b""
            self.last_arrival = None
            self.last_sample = (0, 0)
            self.fade = True
            self.state = (
                "disconnected"
                if not connected
                else "paused" if status != "playing" else "buffering"
            )
            return True

    # Discard queued audio after capture failure, without changing track identity.
    def capture_lost(self, generation):
        """Discard queued audio after capture failure, without changing track identity."""
        with self.lock:
            if generation != self.generation:
                return
            self.buffer.clear()
            self.tail = b""
            self.capture_available = False
            self.last_arrival = None
            self.fade = True
            if self.connected and self.status == "playing":
                self.state = "buffering"

    # Reject old captures and retain complete interleaved stereo frames.
    def ingest(self, data, now, generation):
        """Reject old captures and retain complete interleaved stereo frames."""
        with self.lock:
            if (
                generation != self.generation
                or not self.connected
                or self.status != "playing"
            ):
                return
            data = self.tail + data
            count = len(data) // 4 * 4
            self.tail = data[count:]
            if not count:
                return
            self.capture_available = True
            if self.last_arrival is not None:
                error = min(
                    250.0,
                    abs((now - self.last_arrival) * 1000 - self.previous_duration),
                )
                self.jitter_ms += 0.1 * (error - self.jitter_ms)
            self.last_arrival = now
            self.previous_duration = count / (self.rate * 4) * 1000
            desired = min(600.0, max(80.0, 80.0 + 4 * self.jitter_ms))
            self.target_ms = (
                max(self.target_ms, desired)
                if desired > self.target_ms
                else max(desired, self.target_ms - 0.2)
            )
            self.buffer.extend(data[:count])
            maximum = self.rate * 4  # Hard cap: one second.
            if len(self.buffer) > maximum:
                drop = (len(self.buffer) - maximum + 3) // 4 * 4
                del self.buffer[:drop]
                self.dropped_frames += drop // 4
                self.fade = True

    # Return exactly 20 ms: current music or bounded, nonblocking silence.
    def pull(self):
        """Return exactly 20 ms: current music or bounded, nonblocking silence."""
        count = self.block_frames * 4
        with self.lock:
            if not self.connected or self.status != "playing":
                self.last_sample = (0, 0)
                return bytes(count)
            target = math.ceil(self.target_ms * self.rate / 1000) * 4
            if self.state == "buffering":
                if len(self.buffer) < target:
                    return bytes(count)
                self.state = "playing"
                self.fade = True
            # Shed complete old stereo frames when local delay has drifted high.
            high = target + self.rate * 4 * 120 // 1000
            if len(self.buffer) > high:
                drop = (len(self.buffer) - target) // 4 * 4
                del self.buffer[:drop]
                self.dropped_frames += drop // 4
                self.fade = True
            if len(self.buffer) < count:
                self.underruns += 1
                self.target_ms = min(600.0, self.target_ms + 40)
                self.state = "buffering"
                self.fade = True
                self.last_sample = (0, 0)
                return bytes(count)
            result = bytearray(self.buffer[:count])
            del self.buffer[:count]
            samples = array.array("h")
            samples.frombytes(result)
            if self.fade:
                fade_frames = min(220, self.block_frames)  # 5 ms de-click ramp.
                for frame in range(fade_frames):
                    fraction = (frame + 1) / fade_frames
                    for channel in (0, 1):
                        i = frame * 2 + channel
                        samples[i] = int(
                            self.last_sample[channel] * (1 - fraction)
                            + samples[i] * fraction
                        )
                self.fade = False
            self.last_sample = (samples[-2], samples[-1])
            return samples.tobytes()

    # Expose local queue delay and state; this excludes host-side buffering.
    def snapshot(self):
        """Expose local queue delay and state; this excludes host-side buffering."""
        with self.lock:
            return {
                "state": self.state,
                "generation": self.generation,
                "source_status": self.status,
                "connected": self.connected,
                "track": self.track,
                "target_ms": round(self.target_ms, 1),
                "pcm_buffered_ms": round(len(self.buffer) / (self.rate * 4) * 1000, 1),
                "jitter_ms": round(self.jitter_ms, 1),
                "underruns": self.underruns,
                "dropped_pcm_frames": self.dropped_frames,
                "transitions": self.transitions,
                "capture_available": self.capture_available,
            }
