package stream

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"sync"
	"time"
)

const frameDuration = 1152 * time.Second / 44100

type timedFrame struct {
	data    []byte
	arrived time.Time
}

// BufferQueue retains whole independent MP3 frames, plus a partial output frame.
type BufferQueue struct {
	lock          sync.Mutex
	readLock      sync.Mutex
	changed       chan struct{}
	frames        []timedFrame
	pending       []byte
	current       []byte
	silence       []byte
	epoch         uint64
	target        int
	priming       bool
	dropped       uint64
	malformed     uint64
	underruns     uint64
	silenceFrames uint64
	realFrames    uint64
}

// frameSize accepts the fixed MPEG-1 Layer III / 44.1 kHz transport used here.
func frameSize(data []byte) int {
	if len(data) < 4 || data[0] != 255 || data[1]&0xfe != 0xfa || data[2]&12 != 0 {
		return 0
	}
	rates := [...]int{0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0}
	bitrate := rates[data[2]>>4]
	if bitrate == 0 {
		return 0
	}
	return 144000*bitrate/44100 + int((data[2]>>1)&1)
}

// independent checks main_data_begin, which must be zero when dropping old frames.
func independent(data []byte) bool {
	if len(data) < 6 {
		return false
	}
	offset := 4
	if data[1]&1 == 0 {
		offset += 2
	}
	return len(data) > offset+1 && data[offset] == 0 && data[offset+1]&0x80 == 0
}

// newQueue is also used by deterministic tests without opening any network ports.
func newQueue(silence []byte) *BufferQueue {
	if frameSize(silence) != len(silence) || !independent(silence) {
		panic("invalid independent silence MP3 frame")
	}
	return &BufferQueue{silence: append([]byte(nil), silence...), target: 8, priming: true, changed: make(chan struct{}, 1)}
}

// NewBufferQueue requires a valid, reservoir-free silent frame for bounded reads.
func NewBufferQueue() *BufferQueue {
	path := os.Getenv("RADIONAV_SILENCE_FILE")
	if path == "" {
		path = "/opt/radionav-usb/silence.mp3"
	}
	data, err := os.ReadFile(path)
	if err != nil {
		panic(err)
	}
	var silence []byte
	for i := 0; i+4 <= len(data); {
		size := frameSize(data[i:])
		if size == 0 || i+size > len(data) {
			i++
			continue
		}
		if independent(data[i : i+size]) {
			silence = append([]byte(nil), data[i:i+size]...)
		}
		i += size
	}
	return newQueue(silence)
}

// BeginSession rejects old producer bytes while preserving an in-flight output frame.
func (q *BufferQueue) BeginSession() uint64 {
	q.lock.Lock()
	defer q.lock.Unlock()
	q.epoch++
	q.frames = nil
	q.pending = nil
	q.priming = true
	q.notify()
	return q.epoch
}

// PutFor assembles fragmented socket chunks without ever splitting queued MP3 frames.
func (q *BufferQueue) PutFor(epoch uint64, data []byte) {
	q.lock.Lock()
	defer q.lock.Unlock()
	if epoch != q.epoch {
		return
	}
	q.pending = append(q.pending, data...)
	now := time.Now()
	for len(q.pending) >= 4 {
		size := frameSize(q.pending)
		if size == 0 {
			q.pending = q.pending[1:]
			q.malformed++
			continue
		}
		if len(q.pending) < size {
			break
		}
		if independent(q.pending[:size]) {
			q.frames = append(q.frames, timedFrame{append([]byte(nil), q.pending[:size]...), now})
		} else {
			q.malformed++
		}
		q.pending = q.pending[size:]
	}
	q.trim(now)
	q.notify()
}

// Put keeps the original producer API usable for tests and utilities.
func (q *BufferQueue) Put(data []byte) {
	q.lock.Lock()
	epoch := q.epoch
	q.lock.Unlock()
	q.PutFor(epoch, data)
}

// trim bounds both frame count and age, including long periods without USB reads.
func (q *BufferQueue) trim(now time.Time) {
	maximum := q.target + 8
	if maximum > 54 {
		maximum = 54
	}
	for len(q.frames) > 0 && (len(q.frames) > maximum ||
		now.Sub(q.frames[0].arrived) > time.Duration(maximum)*frameDuration) {
		q.frames = q.frames[1:]
		q.dropped++
	}
}

// notify wakes a waiting reader; the queue lock protects the state it will inspect.
func (q *BufferQueue) notify() {
	select {
	case q.changed <- struct{}{}:
	default:
	}
}

// Get retains the original API while waiting for future producer frames.
func (q *BufferQueue) Get(size int) []byte {
	data, _ := q.GetContext(context.Background(), size)
	return data
}

// GetContext handles host read-ahead without manufacturing seconds of future silence.
func (q *BufferQueue) GetContext(ctx context.Context, size int) ([]byte, error) {
	q.readLock.Lock()
	defer q.readLock.Unlock()
	if size <= 0 {
		return []byte{}, nil
	}
	result := make([]byte, size)
	var waitUntil time.Time
	for offset := 0; offset < size; {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		q.lock.Lock()
		if len(q.current) > 0 {
			n := copy(result[offset:], q.current)
			q.current = q.current[n:]
			offset += n
			q.lock.Unlock()
			if q.priming {
				waitUntil = time.Now().Add(frameDuration)
			} else {
				waitUntil = time.Time{}
			}
			continue
		}
		now := time.Now()
		q.trim(now)
		if q.priming && len(q.frames) >= q.target {
			q.priming = false
		}
		if !q.priming && len(q.frames) > 0 {
			q.current = q.frames[0].data
			q.frames = q.frames[1:]
			q.realFrames++
			if q.realFrames%256 == 0 && q.target > 8 {
				q.target--
			}
			q.lock.Unlock()
			continue
		}
		if waitUntil.IsZero() {
			// LAME and TCP may deliver several frames together. Let the producer
			// catch up instead of treating a fast USB request as an audio underrun.
			grace := time.Second
			if q.priming {
				grace += time.Duration(q.target) * frameDuration
			}
			waitUntil = now.Add(grace)
		}
		if !now.Before(waitUntil) {
			if !q.priming {
				q.underruns++
				q.target += 2
				if q.target > 46 {
					q.target = 46
				}
				q.priming = true
			}
			q.current = q.silence
			q.silenceFrames++
			q.lock.Unlock()
			continue
		}
		remaining := time.Until(waitUntil)
		q.lock.Unlock()
		timer := time.NewTimer(remaining)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, ctx.Err()
		case <-q.changed:
			timer.Stop()
		case <-timer.C:
		}
	}
	return result, nil
}

// Has is immediately true because a silent fallback is available at startup.
func (q *BufferQueue) Has(_ int) bool { return true }

// WaitFor is retained for API compatibility; no read waits on a future producer.
func (q *BufferQueue) WaitFor(_ int) <-chan bool {
	ready := make(chan bool)
	close(ready)
	return ready
}

// Size describes queued music bytes, excluding synthetic silence not yet requested.
func (q *BufferQueue) Size() int {
	q.lock.Lock()
	defer q.lock.Unlock()
	size := len(q.current)
	for _, frame := range q.frames {
		size += len(frame.data)
	}
	return size
}

// Empty reports whether queued music remains.
func (q *BufferQueue) Empty() bool { return q.Size() == 0 }

// Stats provides local delay estimates; it cannot observe the USB host's own cache.
func (q *BufferQueue) Stats() map[string]interface{} {
	q.lock.Lock()
	defer q.lock.Unlock()
	return map[string]interface{}{
		"generation": q.epoch, "queued_frames": len(q.frames),
		"queued_ms": float64(len(q.frames)) * 1152 * 1000 / 44100,
		"target_ms": float64(q.target) * 1152 * 1000 / 44100,
		"priming":   q.priming, "dropped_frames": q.dropped,
		"malformed_frames": q.malformed, "underruns": q.underruns,
		"silence_frames": q.silenceFrames, "music_frames": q.realFrames,
	}
}

// StatsHandler exposes only read-only local observability.
func (q *BufferQueue) StatsHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" || r.URL.Path != "/stats" {
			http.Error(w, "not found", 404)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(q.Stats())
	})
}
