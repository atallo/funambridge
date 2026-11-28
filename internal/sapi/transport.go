package sapi

import (
	"crypto/tls"
	"net/http"
	"time"
)

// O2 sits behind CloudFront, which rejects TLS clients whose handshake does
// not announce a protocol (ALPN): they get an empty 401 on every request, even
// with a valid session (seen in Nov 2026 with Python in Docker; checked again
// with internal/sapi/live_tls_test.go). Go's standard TLS works as long as it
// announces one, so the transport says "http/1.1" explicitly.

// FingerprintName describes the TLS client in use (for the log / panel).
func FingerprintName() string { return "TLS de Go (ALPN http/1.1)" }

func tlsConfig() *tls.Config {
	return &tls.Config{NextProtos: []string{"http/1.1"}, MinVersion: tls.VersionTLS12}
}

// newTransport builds an HTTP/1.1 transport.
//   - keepAlive: reuse connections (API calls, downloads). Uploads use a
//     transport without it: a big body must never go out on a pooled socket
//     the server already closed.
//   - headerWait: how long to wait for the response once the request is sent.
func newTransport(keepAlive bool, headerWait time.Duration) *http.Transport {
	return &http.Transport{
		DialContext:           newDialer().DialContext,
		TLSClientConfig:       tlsConfig(),
		ForceAttemptHTTP2:     false,
		TLSNextProto:          map[string]func(string, *tls.Conn) http.RoundTripper{}, // never HTTP/2
		DisableKeepAlives:     !keepAlive,
		MaxIdleConns:          64,
		MaxIdleConnsPerHost:   16,
		IdleConnTimeout:       50 * time.Second,
		TLSHandshakeTimeout:   20 * time.Second,
		ResponseHeaderTimeout: headerWait,
		ExpectContinueTimeout: 0,
		DisableCompression:    true,
		Proxy:                 http.ProxyFromEnvironment,
	}
}

var (
	apiTransport    = newTransport(true, 90*time.Second)
	uploadTransport = newTransport(false, 5*time.Minute) // O2 may take minutes to answer big files
)

// LoginTransport is the transport for the SMS login (Telefónica hosts).
func LoginTransport() http.RoundTripper { return apiTransport }
