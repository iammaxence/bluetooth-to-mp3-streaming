package stream

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"math"
	"net"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func testFrame(marker byte) []byte {
	frame := make([]byte, 417)
	copy(frame, []byte{255, 251, 144, 0})
	frame[40] = marker
	return frame
}
func feed(q *BufferQueue, epoch uint64, marker byte, count int) {
	for i := 0; i < count; i++ {
		q.PutFor(epoch, testFrame(marker))
	}
}
func TestSilenceWithoutProducer(t *testing.T) {
	q := newQueue(testFrame(0))
	got := q.Get(417*100 + 17)
	if len(got) != 417*100+17 {
		t.Fatal("short read")
	}
	for i := 0; i < 100; i++ {
		if !bytes.Equal(got[i*417:(i+1)*417], testFrame(0)) {
			t.Fatal("invalid silence framing")
		}
	}
}
func TestPartialFrameSurvivesTrackChange(t *testing.T) {
	q := newQueue(testFrame(0))
	e := q.BeginSession()
	feed(q, e, 1, 10)
	prefix := q.Get(10)
	e = q.BeginSession()
	feed(q, e, 2, 10)
	first := append(prefix, q.Get(407)...)
	if !bytes.Equal(first, testFrame(1)) {
		t.Fatal("cut in-flight MP3 frame")
	}
	if !bytes.Equal(q.Get(417), testFrame(2)) {
		t.Fatal("old track leaked")
	}
}
func TestOldProducerRejected(t *testing.T) {
	q := newQueue(testFrame(0))
	old := q.BeginSession()
	q.BeginSession()
	feed(q, old, 1, 10)
	if q.Size() != 0 {
		t.Fatal("accepted old producer")
	}
}
func TestFragmentedInput(t *testing.T) {
	q := newQueue(testFrame(0))
	e := q.BeginSession()
	frame := testFrame(3)
	for _, b := range frame {
		q.PutFor(e, []byte{b})
	}
	if q.Size() != 417 {
		t.Fatal("fragmented frame not reconstructed")
	}
}
func TestReservoirDependentFrameRejected(t *testing.T) {
	q := newQueue(testFrame(0))
	frame := testFrame(3)
	frame[4] = 1
	q.Put(frame)
	if q.Size() != 0 || q.malformed != 1 {
		t.Fatal("accepted dependent MP3 frame")
	}
}
func TestBacklogIsBounded(t *testing.T) {
	q := newQueue(testFrame(0))
	feed(q, 0, 1, 10000)
	if len(q.frames) > 16 || q.dropped == 0 {
		t.Fatal("unbounded queue")
	}
}
func TestExpiredMusicIsNotReplayed(t *testing.T) {
	q := newQueue(testFrame(0))
	feed(q, 0, 1, 10)
	for i := range q.frames {
		q.frames[i].arrived = time.Now().Add(-time.Minute)
	}
	if !bytes.Equal(q.Get(417), testFrame(0)) {
		t.Fatal("stale audio played")
	}
}
func TestUnderrunAdaptsOnceThenSilence(t *testing.T) {
	q := newQueue(testFrame(0))
	feed(q, 0, 1, 8)
	q.Get(417 * 20)
	if q.underruns != 1 || q.target != 10 {
		t.Fatal("underrun not handled", q.Stats())
	}
	q.Get(417 * 20)
	if q.underruns != 1 {
		t.Fatal("repeated underrun count during silence")
	}
}
func TestStatisticsAreReadOnly(t *testing.T) {
	q := newQueue(testFrame(0))
	recorder := httptest.NewRecorder()
	q.StatsHandler().ServeHTTP(recorder, httptest.NewRequest("GET", "/stats", nil))
	var result map[string]interface{}
	if json.Unmarshal(recorder.Body.Bytes(), &result) != nil || recorder.Code != 200 {
		t.Fatal("bad stats")
	}
	recorder = httptest.NewRecorder()
	q.StatsHandler().ServeHTTP(recorder, httptest.NewRequest("POST", "/stats", nil))
	if recorder.Code != 404 {
		t.Fatal("unexpected mutation endpoint")
	}
}
func TestReadAcrossStreamingBoundaries(t *testing.T) {
	path := filepath.Join(t.TempDir(), "disk")
	os.WriteFile(path, bytes.Repeat([]byte{'X'}, 1000), 0600)
	source, _ := os.Open(path)
	defer source.Close()
	stat, _ := source.Stat()
	f := &File{original: source, stat: stat, offsetMin: 100, offsetMax: 300, bufferQueue: newQueue(testFrame(0))}
	result, err := f.Read(context.Background(), make([]byte, 230), 90)
	if err != 0 {
		t.Fatal(err)
	}
	data, status := result.Bytes(make([]byte, 230))
	if status != 0 || len(data) != 230 || !bytes.Equal(data[:10], bytes.Repeat([]byte{'X'}, 10)) ||
		!bytes.Equal(data[10:211], testFrame(0)[:201]) ||
		!bytes.Equal(data[211:], bytes.Repeat([]byte{'X'}, 19)) {
		t.Fatal("boundary corruption")
	}
}
func TestCancelledReadAndEOF(t *testing.T) {
	path := filepath.Join(t.TempDir(), "disk")
	os.WriteFile(path, []byte("123"), 0600)
	source, _ := os.Open(path)
	defer source.Close()
	stat, _ := source.Stat()
	f := &File{original: source, stat: stat, offsetMin: 100, offsetMax: 200, bufferQueue: newQueue(testFrame(0))}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := f.Read(ctx, make([]byte, 4), 0); err != syscall.EINTR {
		t.Fatal("cancellation ignored")
	}
	result, err := f.Read(context.Background(), make([]byte, 4), 3)
	if err != 0 || result.Size() != 0 {
		t.Fatal("bad EOF")
	}
}
func TestReceiverSessionResetAndStop(t *testing.T) {
	t.Setenv("RADIONAV_STATS_ADDR", "off")
	q := newQueue(testFrame(0))
	r := NewReceiver(q, 0).(*receiver)
	if err := r.Start(); err != nil {
		t.Fatal(err)
	}
	defer r.Stop()
	conn, err := net.Dial("tcp", r.listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	conn.Write(testFrame(7))
	deadline := time.Now().Add(2 * time.Second)
	for q.Size() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if q.Size() != 417 {
		t.Fatal("producer bytes missing")
	}
	conn.Close()
	second, err := net.Dial("tcp", r.listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	second.Write(testFrame(8))
	deadline = time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		q.lock.Lock()
		ok := q.epoch == 2 && len(q.frames) == 1 && q.frames[0].data[40] == 8
		q.lock.Unlock()
		if ok {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("new producer failed to clear old track")
}

func TestRealEncoderToQueueToDecoder(t *testing.T) {
	if _, err := exec.LookPath("lame"); err != nil {
		t.Skip("LAME not installed on this test host")
	}
	encode := func(tone bool) []byte {
		pcm := make([]byte, 44100*4*2)
		if tone {
			for i := 0; i < 44100*2; i++ {
				sample := int16(3000 * math.Sin(2*math.Pi*440*float64(i)/44100))
				binary.LittleEndian.PutUint16(pcm[i*4:], uint16(sample))
				binary.LittleEndian.PutUint16(pcm[i*4+2:], uint16(sample))
			}
		}
		cmd := exec.Command("lame", "--silent", "--nores", "--noreplaygain", "-r", "-s", "44.1", "--bitwidth", "16", "--signed", "--little-endian", "-m", "s", "-b", "128", "-q", "7", "-", "-")
		cmd.Stdin = bytes.NewReader(pcm)
		data, err := cmd.Output()
		if err != nil {
			t.Fatal(err)
		}
		return data
	}
	silenceBytes := encode(false)
	var silence []byte
	for i := 0; i+4 <= len(silenceBytes); {
		n := frameSize(silenceBytes[i:])
		if n == 0 || i+n > len(silenceBytes) {
			i++
			continue
		}
		if independent(silenceBytes[i : i+n]) {
			silence = append([]byte(nil), silenceBytes[i:i+n]...)
		}
		i += n
	}
	q := newQueue(silence)
	epoch := q.BeginSession()
	tone := encode(true)
	// Uneven network chunk boundaries must not affect MP3 frame assembly.
	for i := 0; i < len(tone); i += 137 {
		end := i + 137
		if end > len(tone) {
			end = len(tone)
		}
		q.PutFor(epoch, tone[i:end])
	}
	output := q.Get(65536)
	path := filepath.Join(t.TempDir(), "stream.mp3")
	os.WriteFile(path, output, 0600)
	wave := path + ".wav"
	cmd := exec.Command("lame", "--silent", "--decode", path, wave)
	if result, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("decode: %v %s", err, result)
	}
	decoded, err := os.ReadFile(wave)
	if err != nil {
		t.Fatal(err)
	}
	var audio []byte
	for i := 12; i+8 <= len(decoded); {
		size := int(binary.LittleEndian.Uint32(decoded[i+4 : i+8]))
		if size < 0 || i+8+size > len(decoded) {
			break
		}
		if string(decoded[i:i+4]) == "data" {
			audio = decoded[i+8 : i+8+size]
			break
		}
		i += 8 + size + (size % 2)
	}
	if len(audio) < 44100 {
		t.Fatal("decoder produced too little PCM")
	}
	nonzero := 0
	for i := 0; i+2 <= len(audio); i += 2 {
		if int16(binary.LittleEndian.Uint16(audio[i:])) != 0 {
			nonzero++
		}
	}
	if nonzero < 1000 {
		t.Fatal("music disappeared")
	}
	tail := audio[len(audio)-22050:]
	energy := 0.0
	for i := 0; i+2 <= len(tail); i += 2 {
		sample := float64(int16(binary.LittleEndian.Uint16(tail[i:])))
		energy += sample * sample
	}
	if math.Sqrt(energy/float64(len(tail)/2)) > 2 {
		t.Fatal("silence fallback is not silent")
	}
}

// TestUSBReadAheadWaitsForMusic reproduces a host asking for future audio in one read.
func TestUSBReadAheadWaitsForMusic(t *testing.T) {
	q := newQueue(testFrame(0))
	epoch := q.BeginSession()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	done := make(chan struct{})
	go func() {
		defer close(done)
		for i := 0; i < 100; i++ {
			q.PutFor(epoch, testFrame(9))
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Millisecond):
			}
		}
	}()
	result, err := q.GetContext(ctx, 417*100)
	if err != nil {
		t.Fatal(err)
	}
	<-done
	for i := 0; i < 100; i++ {
		if !bytes.Equal(result[i*417:(i+1)*417], testFrame(9)) {
			t.Fatalf("future music replaced by silence at frame %d: %v", i, q.Stats())
		}
	}
	if q.silenceFrames != 0 {
		t.Fatal("manufactured silence during a live producer", q.Stats())
	}
}

// TestWaitingReadCanBeCancelled checks unplug/shutdown does not wait for a producer.
func TestWaitingReadCanBeCancelled(t *testing.T) {
	q := newQueue(testFrame(0))
	ctx, cancel := context.WithCancel(context.Background())
	finished := make(chan error, 1)
	go func() { _, err := q.GetContext(ctx, 131072); finished <- err }()
	time.Sleep(20 * time.Millisecond)
	cancel()
	select {
	case err := <-finished:
		if err != context.Canceled {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("waiting USB read ignored cancellation")
	}
}

// TestUSBReadAheadWithEncoderBursts models buffered encoder stdout, not individual frames.
func TestUSBReadAheadWithEncoderBursts(t *testing.T) {
	q := newQueue(testFrame(0))
	epoch := q.BeginSession()
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	go func() {
		for batch := 0; batch < 10; batch++ {
			feed(q, epoch, 6, 10)
			select {
			case <-ctx.Done():
				return
			case <-time.After(350 * time.Millisecond):
			}
		}
	}()
	data, err := q.GetContext(ctx, 417*100)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 100; i++ {
		if !bytes.Equal(data[i*417:(i+1)*417], testFrame(6)) {
			t.Fatalf("encoder burst gap replaced by silence at frame %d: %v", i, q.Stats())
		}
	}
}
