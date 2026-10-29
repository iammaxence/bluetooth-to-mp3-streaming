# Verify adaptive PCM state changes, buffering limits, and audio transitions.
import array
import unittest
from engine import Engine


# Generate interleaved stereo PCM samples for deterministic tests.
def pcm(frames, sample=1000):
    return array.array("h", [sample, -sample] * frames).tobytes()


class EngineTests(unittest.TestCase):
    # Create an engine connected to a simulated playing track.
    def playing(self):
        engine = Engine()
        engine.transition(True, "playing", {"Title": "one"})
        return engine

    # Feed sample PCM into the engine's current capture generation.
    def fill(self, engine, frames=12000, now=0):
        engine.ingest(pcm(frames), now, engine.generation)

    # Verify that a disconnected source produces one complete silent block.
    def test_disconnected_outputs_exact_silence(self):
        engine = Engine()
        self.assertEqual(engine.pull(), bytes(3528))

    # Verify that playback waits for enough PCM before starting.
    def test_prebuffer_before_start(self):
        engine = self.playing()
        self.fill(engine, 100)
        self.assertEqual(engine.pull(), bytes(3528))
        self.assertEqual(engine.snapshot()["state"], "buffering")
        self.fill(engine)
        self.assertNotEqual(engine.pull(), bytes(3528))
        self.assertEqual(engine.snapshot()["state"], "playing")

    # Verify that pausing discards both queued PCM and incomplete frames.
    def test_pause_flushes_music_and_partial_frames(self):
        engine = self.playing()
        self.fill(engine)
        engine.ingest(b"abc", 0, engine.generation)
        engine.transition(True, "paused", {"Title": "one"})
        self.assertEqual(engine.pull(), bytes(3528))
        self.assertEqual(engine.snapshot()["pcm_buffered_ms"], 0)
        self.assertEqual(engine.tail, b"")

    # Verify that PCM arriving during a pause is ignored.
    def test_paused_data_is_rejected(self):
        engine = self.playing()
        engine.transition(True, "paused", None)
        self.fill(engine)
        self.assertEqual(engine.buffer, b"")

    # Verify that resumed playback waits for newly captured audio.
    def test_resume_prebuffers_fresh_audio(self):
        engine = self.playing()
        self.fill(engine)
        engine.transition(True, "paused", {"Title": "one"})
        engine.transition(True, "playing", {"Title": "one"})
        self.assertEqual(engine.pull(), bytes(3528))
        self.fill(engine, 12000)
        self.assertNotEqual(engine.pull(), bytes(3528))

    # Verify that a track change rejects data from the old capture generation.
    def test_track_change_invalidates_inflight_capture(self):
        engine = self.playing()
        old = engine.generation
        self.fill(engine)
        engine.transition(True, "playing", {"Title": "two"})
        engine.ingest(pcm(20000), 1, old)
        self.assertEqual(engine.buffer, b"")
        self.assertEqual(engine.pull(), bytes(3528))

    # Verify that repeated status notifications preserve the current generation.
    def test_duplicate_status_does_not_restart_encoder(self):
        engine = self.playing()
        generation = engine.generation
        self.assertFalse(engine.transition(True, "playing", {"Title": "one"}))
        self.assertEqual(engine.generation, generation)

    # Verify that disconnecting removes queued music and produces silence.
    def test_disconnect_flushes_pending_music(self):
        engine = self.playing()
        self.fill(engine)
        engine.transition(False, "stopped", None)
        self.assertEqual(engine.pull(), bytes(3528))
        self.assertEqual(engine.buffer, b"")

    # Verify that capture failure clears PCM and requires fresh prebuffering.
    def test_capture_failure_requires_rebuffer(self):
        engine = self.playing()
        self.fill(engine)
        engine.pull()
        engine.capture_lost(engine.generation)
        self.assertEqual(engine.snapshot()["state"], "buffering")
        self.assertEqual(engine.pull(), bytes(3528))

    # Verify that failure of an old capture cannot clear the new track.
    def test_old_capture_failure_does_not_clear_new_track(self):
        engine = self.playing()
        old = engine.generation
        engine.transition(True, "playing", {"Title": "two"})
        self.fill(engine)
        engine.capture_lost(old)
        self.assertTrue(engine.buffer)

    # Verify that partial stereo frames are assembled across capture reads.
    def test_partial_stereo_frames_are_retained(self):
        engine = self.playing()
        engine.ingest(b"abc", 0, engine.generation)
        self.assertEqual(engine.buffer, b"")
        engine.ingest(b"d", 0.1, engine.generation)
        self.assertEqual(engine.buffer, b"abcd")

    # Verify that the hard buffer limit retains complete stereo frames.
    def test_hard_cap_preserves_stereo_alignment(self):
        engine = self.playing()
        engine.ingest(pcm(100000) + b"ab", 0, engine.generation)
        self.assertLessEqual(len(engine.buffer), 44100 * 4)
        self.assertEqual(len(engine.buffer) % 4, 0)
        self.assertGreater(engine.dropped_frames, 0)

    # Verify that playback drops old PCM when the local buffer grows too large.
    def test_latency_excess_is_shed_on_playout(self):
        engine = self.playing()
        self.fill(engine, 40000)
        engine.pull()
        self.assertLess(engine.snapshot()["pcm_buffered_ms"], 200)
        self.assertGreater(engine.dropped_frames, 0)

    # Verify that an underrun raises the target and returns to buffering.
    def test_underrun_raises_target_and_rebuffers(self):
        engine = self.playing()
        self.fill(engine)
        engine.pull()
        target = engine.target_ms
        for _ in range(30):
            engine.pull()
        self.assertEqual(engine.underruns, 1)
        self.assertGreater(engine.target_ms, target)
        self.assertEqual(engine.state, "buffering")

    # Verify that irregular arrivals raise the target within its maximum.
    def test_jitter_target_grows_but_is_bounded(self):
        engine = self.playing()
        for i in range(30):
            engine.ingest(pcm(882), i * 0.2, engine.generation)
        self.assertGreater(engine.target_ms, 160)
        self.assertLessEqual(engine.target_ms, 600)

    # Verify that stable arrivals gradually reduce the buffer target.
    def test_stable_arrivals_slowly_reduce_target(self):
        engine = self.playing()
        engine.target_ms = 300
        for i in range(100):
            engine.ingest(pcm(882), i * 0.02, engine.generation)
            engine.pull()
        self.assertLess(engine.target_ms, 300)

    # Verify that the fade keeps the block size and sample values in range.
    def test_fade_does_not_change_block_size_or_overflow(self):
        engine = self.playing()
        engine.ingest(pcm(12000, 32767), 0, engine.generation)
        result = array.array("h")
        result.frombytes(engine.pull())
        self.assertEqual(len(result), 1764)
        self.assertLess(abs(result[0]), 32767)
        self.assertEqual(result[440], 32767)


if __name__ == "__main__":
    unittest.main()
