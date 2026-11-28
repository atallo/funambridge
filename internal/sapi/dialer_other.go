//go:build !linux

package sapi

import (
	"net"
	"time"
)

// Outside Linux there is no TCP_USER_TIMEOUT; TCP keepalives still detect a
// dead peer (more slowly).
func newDialer() *net.Dialer {
	return &net.Dialer{Timeout: 30 * time.Second, KeepAlive: 15 * time.Second}
}
