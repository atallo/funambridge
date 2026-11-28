//go:build linux

package sapi

import (
	"net"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

// tcpUserTimeout: if what we sent is not acknowledged within this time the
// kernel declares the connection dead (cable pulled, NAT entry gone...).
// Without it Linux keeps retrying for ~15 minutes while a transfer hangs.
const tcpUserTimeout = 60 * time.Second

func newDialer() *net.Dialer {
	return &net.Dialer{
		Timeout:   30 * time.Second,
		KeepAlive: 15 * time.Second,
		Control: func(_, _ string, c syscall.RawConn) error {
			var serr error
			if err := c.Control(func(fd uintptr) {
				serr = unix.SetsockoptInt(int(fd), unix.IPPROTO_TCP, unix.TCP_USER_TIMEOUT,
					int(tcpUserTimeout/time.Millisecond))
			}); err != nil {
				return err
			}
			return serr
		},
	}
}
