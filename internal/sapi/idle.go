package sapi

import (
	"fmt"
	"io"
	"sync/atomic"
	"time"
)

// DownloadIdle: a download that receives nothing from O2 for this long is cut
// (the client gets a dropped connection and retries) instead of hanging
// forever. TCP_USER_TIMEOUT does not cover this: it only fires when WE send.
var DownloadIdle = 60 * time.Second

// idleReader closes the body when a single Read waits longer than d. Only
// time spent waiting for O2 counts, not the time the client takes to take
// the data.
type idleReader struct {
	rc       io.ReadCloser
	d        time.Duration
	t        *time.Timer
	timedOut atomic.Bool
}

func withIdleTimeout(rc io.ReadCloser, d time.Duration) io.ReadCloser {
	r := &idleReader{rc: rc, d: d}
	r.t = time.AfterFunc(time.Hour, func() {
		r.timedOut.Store(true)
		rc.Close()
	})
	r.t.Stop()
	return r
}

func (r *idleReader) Read(p []byte) (int, error) {
	r.t.Reset(r.d)
	n, err := r.rc.Read(p)
	r.t.Stop()
	if err != nil && err != io.EOF && r.timedOut.Load() {
		err = fmt.Errorf("O2 dejó de enviar datos durante %s: %w", r.d, err)
	}
	return n, err
}

func (r *idleReader) Close() error {
	r.t.Stop()
	return r.rc.Close()
}
