// Package server is funambridge's HTTP server: WebDAV for every path except
// /_admin (the panel). /dav/... is accepted as an alias of /....
package server

import (
	"context"
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"mime"
	"net"
	"net/http"
	"net/url"
	"path"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/atallo/funambridge/internal/config"
	"github.com/atallo/funambridge/internal/davfs"
	"github.com/atallo/funambridge/internal/sapi"
	"github.com/atallo/funambridge/internal/store"
	"github.com/atallo/funambridge/internal/transfers"
)

const (
	AdminPrefix = "/_admin"
	davAlias    = "/dav"
)

type Server struct {
	R *Registry
}

func New(r *Registry) *Server { return &Server{R: r} }

func (s *Server) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	sw := &statusWriter{ResponseWriter: w}
	p := req.URL.Path
	defer func() {
		line, args := "%s - %q %d", []any{clientIP(req), req.Method + " " + req.URL.RequestURI(), sw.code()}
		if strings.HasSuffix(p, ".json") && strings.HasPrefix(p, AdminPrefix) {
			log.Debugf(line, args...) // the panel's own polling
		} else {
			log.Infof(line, args...)
		}
	}()
	if p == "/favicon.ico" {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if p == AdminPrefix || strings.HasPrefix(p, AdminPrefix+"/") {
		if !s.R.Cfg.AdminEnabled {
			http.NotFound(sw, req)
			return
		}
		s.admin(sw, req)
		return
	}
	if p == "/" && req.Method == http.MethodGet && !isBasic(req) &&
		strings.Contains(req.Header.Get("Accept"), "text/html") && s.R.Cfg.AdminEnabled {
		http.Redirect(sw, req, AdminPrefix+"/", http.StatusSeeOther)
		return
	}
	s.webdav(sw, req)
}

func clientIP(r *http.Request) string {
	h, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return h
}

func isBasic(r *http.Request) bool { return strings.HasPrefix(r.Header.Get("Authorization"), "Basic ") }

// account checks HTTP Basic: user = the account's user (or its name), password
// = its password; both must match.
func (s *Server) account(r *http.Request) *config.Account {
	a := r.Header.Get("Authorization")
	if !strings.HasPrefix(a, "Basic ") {
		return nil
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(a[6:]))
	if err != nil {
		return nil
	}
	user, pw, _ := strings.Cut(string(raw), ":")
	acc := s.R.Cfg.ByUser(user)
	if acc == nil || subtle.ConstantTimeCompare([]byte(pw), []byte(acc.SecretKey)) != 1 {
		return nil
	}
	return acc
}

var mtimeHdr = regexp.MustCompile(`^\d{9,11}$`)

func (s *Server) webdav(w http.ResponseWriter, r *http.Request) {
	prefix := ""
	if r.URL.Path == davAlias || strings.HasPrefix(r.URL.Path, davAlias+"/") {
		prefix = davAlias
	}
	if r.Method == http.MethodOptions && !isBasic(r) {
		w.Header().Set("DAV", "1, 2")
		w.Header().Set("MS-Author-Via", "DAV")
		w.Header().Set("Allow", "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, PROPPATCH, MKCOL, COPY, MOVE, LOCK, UNLOCK")
		w.WriteHeader(http.StatusOK)
		return
	}
	acc := s.account(r)
	if acc == nil {
		w.Header().Set("WWW-Authenticate", `Basic realm="funambridge"`)
		http.Error(w, "Unauthorized", http.StatusUnauthorized)
		return
	}
	if !acc.HasAuth() {
		http.Error(w, "la cuenta no tiene sesión de O2: inicia sesión desde el panel", http.StatusForbidden)
		return
	}
	live := s.R.For(acc)
	ctx := davfs.WithHolder(r.Context())

	if r.Method == http.MethodPut {
		lim := live.Store.C.Limits(ctx)
		if r.ContentLength > lim.MaxUpload {
			w.Header().Set("Connection", "close")
			http.Error(w, fmt.Sprintf("O2 Cloud no admite ficheros de más de %d MB", lim.MaxUpload>>20),
				http.StatusRequestEntityTooLarge)
			return
		}
		if v := strings.TrimSpace(r.Header.Get("X-OC-Mtime")); mtimeHdr.MatchString(v) {
			sec, _ := strconv.ParseInt(v, 10, 64)
			ctx = davfs.WithMtime(ctx, time.Unix(sec, 0))
		}
	}
	r = r.WithContext(ctx)
	rel := strings.TrimPrefix(r.URL.Path, prefix)

	switch r.Method {
	case http.MethodGet, http.MethodHead:
		if s.serveFile(w, r, live, acc, rel) {
			return
		}
	case "COPY":
		if s.copyFile(w, r, live, acc, prefix, rel) {
			return
		}
	}
	ew := &errWriter{ResponseWriter: w, ctx: ctx, s: s, acc: acc}
	live.handler(prefix).ServeHTTP(ew, r)
}

// fail answers an O2 error with what it means for the client.
func (s *Server) fail(w http.ResponseWriter, acc *config.Account, err error) {
	code, retry, msg := s.mapErr(acc, err)
	if retry != "" {
		w.Header().Set("Retry-After", retry)
	}
	http.Error(w, msg, code)
}

func (s *Server) mapErr(acc *config.Account, err error) (int, string, string) {
	k, isSapi := sapi.KindOf(err)
	switch {
	case errors.Is(err, sapi.ErrTooLarge):
		return http.StatusRequestEntityTooLarge, "", "O2 Cloud: fichero demasiado grande"
	case errors.Is(err, sapi.ErrUnsupported):
		return http.StatusUnsupportedMediaType, "", "O2 Cloud no admite ese contenido"
	case errors.Is(err, store.ErrNotFound):
		return http.StatusNotFound, "", "Not found"
	case isSapi && k == sapi.KindSession:
		if acc != nil {
			s.R.SessionDead(acc, err)
		}
		// 503 + Retry-After: the backup client retries while the session is
		// renewed from the panel (a 403 makes Hyper Backup give up at once).
		return http.StatusServiceUnavailable, "60",
			"la sesión de O2 ha caducado: renuévala en el panel de funambridge"
	case isSapi && (k == sapi.KindTransient || k == sapi.KindEdge),
		errors.Is(err, sapi.ErrValidating), errors.Is(err, sapi.ErrFolderTooBig),
		errors.Is(err, context.DeadlineExceeded):
		return http.StatusServiceUnavailable, "30", "O2 Cloud no disponible ahora: " + err.Error()
	case isSapi && k == sapi.KindNotFound:
		return http.StatusNotFound, "", "Not found"
	case isSapi:
		return http.StatusBadGateway, "", "O2 Cloud rechazó la operación: " + err.Error()
	}
	return http.StatusInternalServerError, "", err.Error()
}

// ---- GET / HEAD of files: exact ranges streamed from O2 -------------------------

var rangeRe = regexp.MustCompile(`^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$`)

// parseRange returns (start, end, ok, bad). ok=false: whole file. Suffix ranges
// ("bytes=-N") become absolute (O2 itself gets them wrong); multi-ranges are
// answered with the whole file.
func parseRange(h string, size int64) (int64, int64, bool, bool) {
	if h == "" {
		return 0, size - 1, false, false
	}
	m := rangeRe.FindStringSubmatch(h)
	if m == nil || (m[1] == "" && m[2] == "") {
		return 0, size - 1, false, false
	}
	if m[1] == "" {
		n, _ := strconv.ParseInt(m[2], 10, 64)
		if n <= 0 {
			return 0, 0, false, true
		}
		return max(0, size-n), size - 1, true, false
	}
	start, _ := strconv.ParseInt(m[1], 10, 64)
	end := size - 1
	if m[2] != "" {
		end, _ = strconv.ParseInt(m[2], 10, 64)
		end = min(end, size-1)
	}
	if start >= size || start > end {
		return 0, 0, false, true
	}
	return start, end, true, false
}

func httpDate(ms int64) string {
	t := time.Now()
	if ms > 0 {
		t = time.UnixMilli(ms)
	}
	return t.UTC().Format(http.TimeFormat)
}

// serveFile answers GET/HEAD of a file; false = not a file (x/net/webdav
// handles it).
func (s *Server) serveFile(w http.ResponseWriter, r *http.Request, live *Live, acc *config.Account, rel string) bool {
	p := strings.Trim(path.Clean("/"+rel), "/")
	if davfs.IsJunk(p) {
		http.NotFound(w, r)
		return true
	}
	in, err := live.Store.Stat(r.Context(), p)
	if err != nil {
		s.fail(w, acc, err)
		return true
	}
	if in == nil {
		http.NotFound(w, r)
		return true
	}
	if in.Dir {
		return false
	}
	e := in.Entry
	ct := mime.TypeByExtension(strings.ToLower(path.Ext(e.Name)))
	if ct == "" {
		ct = "application/octet-stream"
	}
	start, end, isRange, bad := parseRange(r.Header.Get("Range"), e.Size)
	if bad {
		w.Header().Set("Content-Range", fmt.Sprintf("bytes */%d", e.Size))
		http.Error(w, "Range Not Satisfiable", http.StatusRequestedRangeNotSatisfiable)
		return true
	}
	length := e.Size
	if isRange {
		length = end - start + 1
	}
	h := w.Header()
	h.Set("Content-Type", ct)
	h.Set("Accept-Ranges", "bytes")
	h.Set("Last-Modified", httpDate(e.Modified))
	if e.ETag != "" {
		h.Set("ETag", `"`+strings.ReplaceAll(e.ETag, `"`, "")+`"`)
	}
	if r.Method == http.MethodHead || length == 0 {
		h.Set("Content-Length", strconv.FormatInt(length, 10))
		if isRange {
			h.Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, end, e.Size))
			w.WriteHeader(http.StatusPartialContent)
		} else {
			w.WriteHeader(http.StatusOK)
		}
		return true
	}

	t := s.R.Tr.Start(transfers.Down, acc.Name, p, length)
	var terr error
	defer func() { s.R.Tr.Finish(t, terr) }()
	release, err := s.R.Tr.Acquire(r.Context(), t)
	if err != nil {
		terr = err
		return true
	}
	defer release()
	if live.Store.Settling(e.ID) {
		t.SetPhase("esperando a O2")
		live.Store.WaitSettled(r.Context(), e.ID) // O2 still validating a fresh upload
	}
	t.SetPhase("descargando")
	e2 := end
	if !isRange {
		e2 = -1
	}
	// open upstream BEFORE answering, so an O2 error can still become a 503
	src, err := live.Store.C.Open(r.Context(), e.ID, start, e2)
	if err != nil {
		terr = err
		s.fail(w, acc, err)
		return true
	}
	defer src.Close()
	h.Set("Content-Length", strconv.FormatInt(length, 10))
	if isRange {
		h.Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, end, e.Size))
		w.WriteHeader(http.StatusPartialContent)
	} else {
		w.WriteHeader(http.StatusOK)
	}
	n, err := io.CopyBuffer(&countWriter{w: w, t: t}, io.LimitReader(src, length), make([]byte, 256<<10))
	if n < length {
		terr = fmt.Errorf("descarga cortada (%d de %d B): %v", n, length, err)
		log.Warnf("GET %s: %v", p, terr)
		// the length is already promised: drop the connection so the client retries
		panic(http.ErrAbortHandler)
	}
	return true
}

type countWriter struct {
	w io.Writer
	t *transfers.T
}

func (c *countWriter) Write(b []byte) (int, error) {
	n, err := c.w.Write(b)
	c.t.AddDone(int64(n))
	return n, err
}

// ---- COPY of a file: done inside O2 (no download + upload) -----------------------

func (s *Server) copyFile(w http.ResponseWriter, r *http.Request, live *Live, acc *config.Account, prefix, rel string) bool {
	src := strings.Trim(path.Clean("/"+rel), "/")
	in, err := live.Store.Stat(r.Context(), src)
	if err != nil {
		s.fail(w, acc, err)
		return true
	}
	if in == nil || in.Dir {
		return false // folders: x/net/webdav copies them file by file
	}
	du, err := url.Parse(r.Header.Get("Destination"))
	if err != nil || du.Path == "" {
		http.Error(w, "Destination no válido", http.StatusBadRequest)
		return true
	}
	dst := strings.Trim(path.Clean("/"+strings.TrimPrefix(du.Path, prefix)), "/")
	if dst == src {
		http.Error(w, "origen y destino iguales", http.StatusForbidden)
		return true
	}
	ex, err := live.Store.Stat(r.Context(), dst)
	if err != nil {
		s.fail(w, acc, err)
		return true
	}
	status := http.StatusCreated
	if ex != nil {
		if strings.EqualFold(r.Header.Get("Overwrite"), "F") {
			http.Error(w, "el destino existe", http.StatusPreconditionFailed)
			return true
		}
		if err := live.Store.Delete(r.Context(), dst); err != nil {
			s.fail(w, acc, err)
			return true
		}
		status = http.StatusNoContent
	}
	if err := live.Store.Copy(r.Context(), src, dst); err != nil {
		if errors.Is(err, store.ErrNotFound) {
			http.Error(w, "la carpeta de destino no existe", http.StatusConflict)
			return true
		}
		s.fail(w, acc, err)
		return true
	}
	w.WriteHeader(status)
	return true
}

// ---- response wrappers -------------------------------------------------------------

// errWriter replaces x/net/webdav's generic error answers (405/500...) with
// what the recorded O2 error means.
type errWriter struct {
	http.ResponseWriter
	ctx     context.Context
	s       *Server
	acc     *config.Account
	swallow bool
}

func (e *errWriter) WriteHeader(code int) {
	if code >= 400 && code != http.StatusNotFound && code != http.StatusUnauthorized {
		if err := davfs.ErrOf(e.ctx); err != nil {
			c, retry, msg := e.s.mapErr(e.acc, err)
			h := e.ResponseWriter.Header()
			h.Del("Content-Length")
			h.Set("Content-Type", "text/plain; charset=utf-8")
			if retry != "" {
				h.Set("Retry-After", retry)
			}
			e.ResponseWriter.WriteHeader(c)
			_, _ = io.WriteString(e.ResponseWriter, msg+"\n")
			e.swallow = true
			return
		}
	}
	e.ResponseWriter.WriteHeader(code)
}

func (e *errWriter) Write(b []byte) (int, error) {
	if e.swallow {
		return len(b), nil
	}
	return e.ResponseWriter.Write(b)
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (s *statusWriter) WriteHeader(c int) {
	if s.status == 0 {
		s.status = c
	}
	s.ResponseWriter.WriteHeader(c)
}

func (s *statusWriter) Write(b []byte) (int, error) {
	if s.status == 0 {
		s.status = 200
	}
	return s.ResponseWriter.Write(b)
}

func (s *statusWriter) code() int {
	if s.status == 0 {
		return 200
	}
	return s.status
}

func (s *statusWriter) Flush() {
	if f, ok := s.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}
