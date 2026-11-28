//go:build live

package sapi

import (
	"bytes"
	"context"
	"crypto/tls"
	"io"
	"net"
	"net/http"
	"testing"
	"time"
)

// Can O2 (behind CloudFront) be used with Go's own TLS instead of the Chrome
// fingerprint? Tries each variant with normal requests: session check,
// folder list, an upload (upload host) and a range download, in a throwaway
// folder. Run with -tags live (see live_test.go).
func TestLiveTLSVariants(t *testing.T) {
	base := liveClient(t)
	ctx := context.Background()
	if err := base.Profile(ctx); err != nil {
		t.Fatal(err)
	}
	root, _ := base.RootID(ctx)
	fid, err := base.CreateFolder(ctx, root, "funambridge-pruebas-tls")
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		ms, _ := base.ListMedia(ctx, fid)
		for _, m := range ms {
			_ = base.DeleteMedia(ctx, m.ID)
		}
		_ = base.DeleteFolder(ctx, fid)
	}()

	goTLS := func(alpn []string) *http.Transport {
		return &http.Transport{
			DialTLSContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
				host, _, _ := net.SplitHostPort(addr)
				raw, err := (&net.Dialer{Timeout: 30 * time.Second}).DialContext(ctx, network, addr)
				if err != nil {
					return nil, err
				}
				c := tls.Client(raw, &tls.Config{ServerName: host, NextProtos: alpn})
				if err := c.HandshakeContext(ctx); err != nil {
					raw.Close()
					return nil, err
				}
				return c, nil
			},
			ForceAttemptHTTP2: false,
		}
	}
	variants := []struct {
		name string
		tr   http.RoundTripper
	}{
		{"A: TLS de Go, ALPN http/1.1", goTLS([]string{"http/1.1"})},
		{"B: TLS de Go, sin ALPN", goTLS(nil)},
		{"C: cliente HTTP de Go por defecto (HTTP/2)", http.DefaultTransport.(*http.Transport).Clone()},
	}
	for i, v := range variants {
		sess := *base.sess
		c := New(base.Base, base.DeviceID, &sess, nil)
		c.api.Transport, c.upload.Transport = v.tr, v.tr
		step := func(what string, err error) bool {
			if err != nil {
				k, _ := KindOf(err)
				t.Logf("%s · %s: FALLA (%s) %v", v.name, what, k, err)
				return false
			}
			t.Logf("%s · %s: OK", v.name, what)
			return true
		}
		if !step("sesión (profile)", c.Profile(ctx)) {
			continue
		}
		_, err := c.AllFolders(ctx)
		if !step("listar carpetas", err) {
			continue
		}
		data := bytes.Repeat([]byte{byte('a' + i)}, 50_000)
		r, err := c.Upload(ctx, UploadReq{FolderID: fid, Name: "v" + string(rune('a'+i)) + ".bin", Size: int64(len(data)),
			Open: func(off int64) (io.Reader, error) { return bytes.NewReader(data[off:]), nil }, Attempts: 1})
		if !step("subir (upload.cloud.o2online.es)", err) {
			continue
		}
		c.WaitValidated(ctx, r.ID, fid, time.Minute)
		rc, err := c.Open(ctx, r.ID, 100, 199)
		if step("descargar rango", err) {
			got, _ := io.ReadAll(rc)
			rc.Close()
			if !bytes.Equal(got, data[100:200]) {
				t.Logf("%s · rango con contenido distinto (%d B)", v.name, len(got))
			}
		}
	}
}
