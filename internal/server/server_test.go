package server_test

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/atallo/funambridge/internal/config"
	"github.com/atallo/funambridge/internal/fakeo2"
	"github.com/atallo/funambridge/internal/sapi"
	"github.com/atallo/funambridge/internal/server"
)

type env struct {
	t   *testing.T
	o2  *fakeo2.Server
	srv *httptest.Server
	cfg *config.Config
}

func setup(t *testing.T, tune func(*config.Config)) *env {
	t.Helper()
	sapi.SetBackoffForTests(5 * time.Millisecond)
	o2 := fakeo2.New()
	t.Cleanup(o2.Close)
	cfg, _ := config.Load(filepath.Join(t.TempDir(), "config.yaml"))
	a := cfg.AddAccount("prueba")
	a.BaseURL, a.AccessKey, a.SecretKey, a.DeviceID = o2.URL, "user", "pass", "web-test"
	a.Session = config.Session{JSessionID: "j", ValidationKey: "k1"}
	cfg.CacheDir = t.TempDir()
	if tune != nil {
		tune(cfg)
	}
	srv := httptest.NewServer(server.New(server.NewRegistry(cfg)))
	t.Cleanup(srv.Close)
	return &env{t: t, o2: o2, srv: srv, cfg: cfg}
}

func (e *env) do(method, path string, body []byte, hdr ...string) (*http.Response, []byte) {
	e.t.Helper()
	var rd io.Reader
	if body != nil {
		rd = bytes.NewReader(body)
	}
	req, _ := http.NewRequest(method, e.srv.URL+path, rd)
	req.SetBasicAuth("user", "pass")
	for i := 0; i+1 < len(hdr); i += 2 {
		req.Header.Set(hdr[i], hdr[i+1])
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatalf("%s %s: %v", method, path, err)
	}
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	return resp, b
}

func (e *env) expect(code int, method, path string, body []byte, hdr ...string) []byte {
	e.t.Helper()
	r, b := e.do(method, path, body, hdr...)
	if r.StatusCode != code {
		e.t.Fatalf("%s %s: %d (esperaba %d): %s", method, path, r.StatusCode, code, b)
	}
	return b
}

func rnd(n int) []byte { b := make([]byte, n); _, _ = rand.Read(b); return b }

func (e *env) folder(name string) string {
	e.o2.Mu.Lock()
	defer e.o2.Mu.Unlock()
	for id, f := range e.o2.Folders {
		if f[0] == name {
			return id
		}
	}
	return ""
}

func TestAuth(t *testing.T) {
	e := setup(t, nil)
	req, _ := http.NewRequest("PROPFIND", e.srv.URL+"/", nil)
	req.SetBasicAuth("user", "mal")
	r, _ := http.DefaultClient.Do(req)
	if r.StatusCode != 401 || r.Header.Get("WWW-Authenticate") == "" {
		t.Fatalf("contraseña mala: %d", r.StatusCode)
	}
}

func TestQuotaInPropfind(t *testing.T) {
	e := setup(t, nil)
	b := string(e.expect(207, "PROPFIND", "/", nil, "Depth", "0"))
	if !strings.Contains(b, "quota-available-bytes>998766<") || !strings.Contains(b, "quota-used-bytes>1234<") {
		t.Fatalf("sin cuota: %s", b)
	}
}

func TestPutGetRangesHead(t *testing.T) {
	e := setup(t, nil)
	e.expect(201, "MKCOL", "/a", nil)
	data := rnd(300_000)
	e.expect(201, "PUT", "/a/f.bin", data)
	b := string(e.expect(207, "PROPFIND", "/a/", nil, "Depth", "1"))
	if !strings.Contains(b, "f.bin") || !strings.Contains(b, "<D:getcontentlength>300000<") {
		t.Fatalf("PROPFIND tras PUT: %s", b)
	}
	if got := e.expect(206, "GET", "/a/f.bin", nil, "Range", "bytes=10-19"); !bytes.Equal(got, data[10:20]) {
		t.Fatal("rango 10-19")
	}
	// O2 resolves "bytes=-N" wrongly: the proxy must turn it absolute
	if got := e.expect(206, "GET", "/a/f.bin", nil, "Range", "bytes=-100"); !bytes.Equal(got, data[len(data)-100:]) {
		t.Fatal("rango -100")
	}
	if got := e.expect(200, "GET", "/a/f.bin", nil); !bytes.Equal(got, data) {
		t.Fatal("GET completo")
	}
	e.expect(416, "GET", "/a/f.bin", nil, "Range", "bytes=999999-")
	before := e.o2.Calls["download"]
	r, _ := e.do("HEAD", "/a/f.bin", nil)
	if r.StatusCode != 200 || r.Header.Get("Content-Length") != "300000" || e.o2.Calls["download"] != before {
		t.Fatalf("HEAD: %d %s (descargas %d->%d)", r.StatusCode, r.Header.Get("Content-Length"), before, e.o2.Calls["download"])
	}
}

func TestOverwriteInPlaceAndDuplicates(t *testing.T) {
	e := setup(t, nil)
	// leftovers of old versions, already in O2 when the proxy starts: two
	// copies with the same name
	dd := e.o2.Mkdir(e.o2.Root, "dd")
	e.o2.Put(dd, "d.bin", []byte("x"), 1)
	e.o2.Put(dd, "d.bin", []byte("y"), 2)
	e.expect(201, "PUT", "/dd/d.bin", []byte("nuevo"))
	n := 0
	for _, f := range e.o2.Files(dd) {
		if f.Name == "d.bin" {
			n++
		}
	}
	if n != 1 {
		t.Fatalf("quedan %d copias de d.bin", n)
	}

	e.expect(201, "MKCOL", "/a", nil)
	e.expect(201, "PUT", "/a/f.bin", []byte("uno"))
	fid := e.folder("a")
	id := e.o2.Files(fid)[0].ID
	e.expect(201, "PUT", "/a/f.bin", []byte("dos!"))
	files := e.o2.Files(fid)
	if len(files) != 1 || files[0].ID != id || string(files[0].Data) != "dos!" {
		t.Fatalf("sobrescritura: %+v", files)
	}
}

func TestMoveCopyDelete(t *testing.T) {
	e := setup(t, nil)
	e.expect(201, "MKCOL", "/a", nil)
	e.expect(201, "PUT", "/a/f.bin", []byte("hola"))
	e.expect(201, "MOVE", "/a/f.bin", nil, "Destination", e.srv.URL+"/a/g.bin")
	e.expect(404, "GET", "/a/f.bin", nil)
	e.expect(201, "COPY", "/a/g.bin", nil, "Destination", e.srv.URL+"/a/h.bin")
	if e.o2.Calls["/sapi/media/file?copy"] != 1 {
		t.Fatalf("la copia no se hizo en O2: %v", e.o2.Calls)
	}
	if got := e.expect(200, "GET", "/a/h.bin", nil); string(got) != "hola" {
		t.Fatal("copia")
	}
	e.expect(204, "DELETE", "/a/g.bin", nil)
	e.expect(404, "GET", "/a/g.bin", nil)
}

func TestDeleteBigFolder(t *testing.T) {
	e := setup(t, nil)
	e.expect(201, "MKCOL", "/big", nil)
	fid := e.folder("big")
	for i := 0; i < 5; i++ {
		e.o2.Put(fid, fmt.Sprintf("f%d", i), []byte("x"), 1)
	}
	e.o2.FolderOp = 2 // O2 refuses folders with more than 2 items (FOL-1036)
	e.expect(204, "DELETE", "/big", nil)
	e.expect(404, "PROPFIND", "/big", nil, "Depth", "0")
}

func TestJunkIsAbsorbed(t *testing.T) {
	e := setup(t, nil)
	e.expect(201, "PUT", "/.DS_Store", []byte("basura"))
	if n := len(e.o2.Files(e.o2.Root)); n != 0 {
		t.Fatalf("se subió basura (%d)", n)
	}
}

func TestTooLargeRefusedBeforeBody(t *testing.T) {
	e := setup(t, nil)
	c, err := net.Dial("tcp", strings.TrimPrefix(e.srv.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	fmt.Fprintf(c, "PUT /x.bin HTTP/1.1\r\nHost: x\r\nAuthorization: Basic dXNlcjpwYXNz\r\nContent-Length: %d\r\n\r\n", int64(5)<<30)
	r, err := http.ReadResponse(bufio.NewReader(c), nil)
	if err != nil || r.StatusCode != 413 {
		t.Fatalf("esperaba 413: %v %v", r, err)
	}
}

func TestTransientIs503(t *testing.T) {
	e := setup(t, nil)
	e.o2.FailContent = 1000
	r, _ := e.do("PUT", "/x.bin", []byte("abc"))
	if r.StatusCode != 503 || r.Header.Get("Retry-After") == "" {
		t.Fatalf("esperaba 503 + Retry-After, %d", r.StatusCode)
	}
	if n := len(e.o2.Files(e.o2.Root)); n != 0 {
		t.Fatalf("quedó un fichero a medias (%d)", n)
	}
}

func TestSessionDeadIs503(t *testing.T) {
	e := setup(t, nil)
	e.o2.SessionDead = true
	r, _ := e.do("PROPFIND", "/", nil, "Depth", "1")
	if r.StatusCode != 503 || r.Header.Get("Retry-After") != "60" {
		t.Fatalf("esperaba 503 + Retry-After 60, %d %q", r.StatusCode, r.Header.Get("Retry-After"))
	}
}

func TestKeyRotationDuringPut(t *testing.T) {
	e := setup(t, nil)
	e.o2.RotateNext = true
	e.expect(201, "PUT", "/r.bin", []byte("abc"))
	if e.cfg.Accounts[0].Session.ValidationKey != e.o2.Key {
		t.Fatal("la clave rotada no se guardó en la sesión")
	}
}

func TestParallelismLimit(t *testing.T) {
	e := setup(t, func(c *config.Config) { c.MaxUploads = 1 })
	e.o2.UploadDelay = 150 * time.Millisecond
	var wg sync.WaitGroup
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			e.expect(201, "PUT", fmt.Sprintf("/p%d.bin", i), rnd(1000))
		}(i)
	}
	wg.Wait()
	if e.o2.MaxConcurrent != 1 {
		t.Fatalf("subidas simultáneas: %d (límite 1)", e.o2.MaxConcurrent)
	}
}

func TestReadWaitsForValidation(t *testing.T) {
	e := setup(t, nil)
	e.o2.ValidateFor = 400 * time.Millisecond
	data := rnd(1000)
	e.expect(201, "PUT", "/v.bin", data)
	if got := e.expect(200, "GET", "/v.bin", nil); !bytes.Equal(got, data) {
		t.Fatal("lectura justo después de subir")
	}
}

func TestCacheServesRightAfterUpload(t *testing.T) {
	e := setup(t, nil) // accounts get cache_seconds = 15
	e.o2.ValidateFor = 3 * time.Second
	data := rnd(200_000)
	e.expect(201, "PUT", "/c.bin", data)
	before := e.o2.Calls["download"]
	t0 := time.Now()
	if got := e.expect(206, "GET", "/c.bin", nil, "Range", "bytes=-50"); !bytes.Equal(got, data[len(data)-50:]) {
		t.Fatal("rango desde la caché")
	}
	if got := e.expect(200, "GET", "/c.bin", nil); !bytes.Equal(got, data) {
		t.Fatal("GET desde la caché")
	}
	if el := time.Since(t0); el > time.Second || e.o2.Calls["download"] != before {
		t.Fatalf("no se sirvió desde la caché (%s, descargas %d->%d)", el, before, e.o2.Calls["download"])
	}
	if b := string(e.expect(200, "GET", "/_admin/cache.json", nil)); !strings.Contains(b, "c.bin") {
		t.Fatalf("cache.json: %s", b)
	}
}

func TestNoCacheWaitsForValidation(t *testing.T) {
	e := setup(t, func(c *config.Config) { c.Accounts[0].CacheSeconds = 0 })
	e.o2.ValidateFor = 2 * time.Second
	data := rnd(1000)
	e.expect(201, "PUT", "/n.bin", data)
	t0 := time.Now()
	if got := e.expect(200, "GET", "/n.bin", nil); !bytes.Equal(got, data) {
		t.Fatal("GET")
	}
	if el := time.Since(t0); el < time.Second {
		t.Fatalf("sin caché debería esperar a la validación (%s)", el)
	}
}

func TestUploadsAreGenericByDefault(t *testing.T) {
	e := setup(t, nil)
	e.expect(201, "PUT", "/foto.jpg", rnd(1000))
	if e.o2.Calls["/sapi/upload/file?save-metadata"] != 1 || e.o2.Calls["/sapi/upload/picture?save-metadata"] != 0 {
		t.Fatalf("un .jpg debe subirse como fichero genérico: %v", e.o2.Calls)
	}
	m := setup(t, func(c *config.Config) { c.UploadAsMedia = true })
	m.expect(201, "PUT", "/foto.jpg", rnd(1000))
	if m.o2.Calls["/sapi/upload/picture?save-metadata"] != 1 {
		t.Fatalf("con upload_as_media un .jpg se sube como foto: %v", m.o2.Calls)
	}
}
