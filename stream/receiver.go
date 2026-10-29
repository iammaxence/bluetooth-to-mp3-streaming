package stream

import (
	"fmt"
	"net"
	"net/http"
	"os"
	"sync"
	"time"
)

type Receiver interface {
	Start() error
	Stop()
}
type receiver struct {
	port     uint16
	bq       *BufferQueue
	listener net.Listener
	current  net.Conn
	stats    *http.Server
	lock     sync.Mutex
	stopping bool
}

// NewReceiver binds one bounded MP3 producer stream and an optional local status API.
func NewReceiver(bq *BufferQueue, port uint16) Receiver { return &receiver{bq: bq, port: port} }

// Start owns listeners; each newly accepted connection begins a fresh generation.
func (r *receiver) Start() error {
	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", r.port))
	if err != nil {
		return err
	}
	address := os.Getenv("RADIONAV_STATS_ADDR")
	if address == "" {
		address = "127.0.0.1:5001"
	}
	if address != "off" {
		statusListener, e := net.Listen("tcp", address)
		if e != nil {
			listener.Close()
			return e
		}
		r.stats = &http.Server{Handler: r.bq.StatsHandler(), ReadHeaderTimeout: 2 * time.Second}
		go r.stats.Serve(statusListener)
	}
	r.listener = listener
	go r.accept()
	return nil
}

// accept serializes producer sessions, preventing old track bytes from arriving late.
func (r *receiver) accept() {
	for {
		connection, err := r.listener.Accept()
		if err != nil {
			return
		}
		r.lock.Lock()
		if r.stopping {
			r.lock.Unlock()
			connection.Close()
			return
		}
		r.current = connection
		r.lock.Unlock()
		epoch := r.bq.BeginSession()
		buffer := make([]byte, 8192)
		for {
			connection.SetReadDeadline(time.Now().Add(5 * time.Second))
			n, e := connection.Read(buffer)
			if n > 0 {
				r.bq.PutFor(epoch, buffer[:n])
			}
			if e != nil {
				break
			}
		}
		connection.Close()
		r.lock.Lock()
		r.current = nil
		r.lock.Unlock()
	}
}

// Stop unblocks listeners and the active read; shutdown never leaks a receiver loop.
func (r *receiver) Stop() {
	r.lock.Lock()
	r.stopping = true
	if r.current != nil {
		r.current.Close()
	}
	r.lock.Unlock()
	if r.listener != nil {
		r.listener.Close()
	}
	if r.stats != nil {
		r.stats.Close()
	}
}
