package sapi

import (
	"context"
	"crypto/tls"
	"fmt"
	"net"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	utls "github.com/refraction-networking/utls"
)

// O2 sits behind CloudFront, which fingerprints the TLS ClientHello: in
// Nov 2026 a Python client without ALPN got an empty 401 on every request from
// a Linux container. We present Chrome's ClientHello (uTLS) with ALPN reduced
// to "http/1.1" -- left as Chrome's "h2,http/1.1", CloudFront picks HTTP/2,
// which this transport does not speak. If the uTLS handshake itself fails
// (library vs. server mismatch), the process falls back to Go's own TLS (with
// ALPN http/1.1) and says so in the log.

var tlsFallback atomic.Bool

// FingerprintName is the TLS fingerprint in use (for the log / panel).
func FingerprintName() string {
	if tlsFallback.Load() {
		return "Go crypto/tls (respaldo)"
	}
	return "Chrome (uTLS, HTTP/1.1)"
}

func chromeHTTP1Spec() (utls.ClientHelloSpec, error) {
	spec, err := utls.UTLSIdToSpec(utls.HelloChrome_Auto)
	if err != nil {
		return spec, err
	}
	for _, ext := range spec.Extensions {
		if alpn, ok := ext.(*utls.ALPNExtension); ok {
			alpn.AlpnProtocols = []string{"http/1.1"}
		}
	}
	return spec, nil
}

var fallbackOnce sync.Once

func dialTLS(ctx context.Context, network, addr string) (net.Conn, error) {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		host = addr
	}
	if !tlsFallback.Load() {
		c, err := dialUTLS(ctx, network, addr, host)
		if err == nil {
			return c, nil
		}
		if ctx.Err() != nil || isNetErr(err) {
			return nil, err // the network failed, not the fingerprint
		}
		fallbackOnce.Do(func() {
			tlsFallback.Store(true)
			log.Warnf("la huella TLS de Chrome falló con %s (%v); se usa el TLS de Go", host, err)
		})
	}
	raw, err := newDialer().DialContext(ctx, network, addr)
	if err != nil {
		return nil, err
	}
	tc := tls.Client(raw, &tls.Config{ServerName: host, NextProtos: []string{"http/1.1"}})
	if err := tc.HandshakeContext(ctx); err != nil {
		_ = raw.Close()
		return nil, err
	}
	return tc, nil
}

func dialUTLS(ctx context.Context, network, addr, host string) (net.Conn, error) {
	spec, err := chromeHTTP1Spec()
	if err != nil {
		return nil, fmt.Errorf("spec: %w", err)
	}
	raw, err := newDialer().DialContext(ctx, network, addr)
	if err != nil {
		return nil, &net.OpError{Op: "dial", Net: network, Err: err}
	}
	uc := utls.UClient(raw, &utls.Config{ServerName: host, NextProtos: []string{"http/1.1"}},
		utls.HelloCustom)
	if err := uc.ApplyPreset(&spec); err != nil {
		_ = raw.Close()
		return nil, fmt.Errorf("preset: %w", err)
	}
	if err := uc.HandshakeContext(ctx); err != nil {
		_ = raw.Close()
		if ne, ok := err.(net.Error); ok && ne.Timeout() {
			return nil, &net.OpError{Op: "handshake", Net: network, Err: err}
		}
		return nil, fmt.Errorf("handshake: %w", err)
	}
	if p := uc.ConnectionState().NegotiatedProtocol; p != "" && p != "http/1.1" {
		_ = uc.Close()
		return nil, fmt.Errorf("el servidor eligió %q en vez de http/1.1", p)
	}
	return uc, nil
}

func isNetErr(err error) bool {
	_, ok := err.(*net.OpError)
	return ok
}

// newTransport builds an HTTP/1.1 transport over dialTLS.
//   - keepAlive: reuse connections (API calls, downloads). Uploads use a
//     transport without it: a big body must never go out on a pooled socket
//     the server already closed (O2 answers 408 there).
//   - headerWait: how long to wait for the response once the request is sent.
func newTransport(keepAlive bool, headerWait time.Duration) *http.Transport {
	return &http.Transport{
		DialContext:           newDialer().DialContext,
		DialTLSContext:        dialTLS,
		ForceAttemptHTTP2:     false,
		DisableKeepAlives:     !keepAlive,
		MaxIdleConns:          64,
		MaxIdleConnsPerHost:   16,
		IdleConnTimeout:       50 * time.Second,
		TLSHandshakeTimeout:   20 * time.Second,
		ResponseHeaderTimeout: headerWait,
		ExpectContinueTimeout: 0,
		DisableCompression:    true,
	}
}

var (
	apiTransport    = newTransport(true, 90*time.Second)
	uploadTransport = newTransport(false, 5*time.Minute) // O2 may take minutes to answer big files
)

// LoginTransport is the transport for the SMS login (Telefónica hosts):
// the same browser fingerprint as the rest.
func LoginTransport() http.RoundTripper { return apiTransport }
