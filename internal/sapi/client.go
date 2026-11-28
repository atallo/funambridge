// Package sapi is a client of O2 Cloud's SAPI (Funambol OneMediaHub) using the
// web session obtained by the SMS login. See docs/o2-sapi.md.
package sapi

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/atallo/funambridge/internal/config"
	"github.com/atallo/funambridge/internal/logx"
)

var log = logx.New("sapi")

// apiCallTimeout bounds a whole non-streaming SAPI call (send + answer).
const apiCallTimeout = 2 * time.Minute

// UserAgent matches the Chrome TLS fingerprint the transport presents.
const UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
	"(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"

type Client struct {
	Base     string // https://cloud.o2online.es
	DeviceID string
	// AsMedia says whether to upload pictures/videos/audio with their media
	// kind (O2 may then process them); nil or false = always as generic files.
	AsMedia func() bool

	sess     *config.Session // updated in place when O2 rotates the key
	onChange func()          // persists the config after a rotation

	jar    *cookiejar.Jar
	api    *http.Client
	upload *http.Client

	mu sync.Mutex
	vk string

	infoMu sync.Mutex
	info   map[string]any
	infoAt time.Time

	urlMu sync.Mutex
	urls  map[string]urlHit

	rootMu sync.Mutex
	rootID string
}

type urlHit struct {
	url string
	at  time.Time
}

// New builds a client for a web session. onChange is called (from any
// goroutine) after the session's validation key or JSESSIONID changed.
func New(base, deviceID string, sess *config.Session, onChange func()) *Client {
	jar, _ := cookiejar.New(nil)
	c := &Client{Base: strings.TrimRight(base, "/"), DeviceID: deviceID, sess: sess,
		onChange: onChange, jar: jar, urls: map[string]urlHit{}}
	noRedirect := func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	c.api = &http.Client{Transport: apiTransport, Jar: jar, CheckRedirect: noRedirect}
	c.upload = &http.Client{Transport: uploadTransport, Jar: jar, CheckRedirect: noRedirect}
	if sess != nil && sess.IsSet() {
		c.setCookie("JSESSIONID", sess.JSessionID)
		c.setCookie("validationKey", sess.ValidationKey)
		if sess.PLC != "" {
			c.setCookie("PLC", sess.PLC)
		}
		c.vk = sess.ValidationKey
	}
	return c
}

func (c *Client) baseURL() *url.URL {
	u, _ := url.Parse(c.Base + "/")
	return u
}

// setCookie stores a cookie for the web host AND its subdomains (the upload
// host upload.<host> needs the same session cookies).
func (c *Client) setCookie(name, value string) {
	u := c.baseURL()
	c.jar.SetCookies(u, []*http.Cookie{{Name: name, Value: value, Path: "/",
		Domain: u.Hostname(), Secure: true}})
}

func (c *Client) cookie(name string) string {
	for _, ck := range c.jar.Cookies(c.baseURL()) {
		if ck.Name == name {
			return ck.Value
		}
	}
	return ""
}

func (c *Client) key() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.vk
}

// adoptKey switches to a rotated validation key (query param + cookie) and
// persists it with the session.
func (c *Client) adoptKey(k string) {
	if k == "" {
		return
	}
	c.mu.Lock()
	changed := k != c.vk
	c.vk = k
	c.mu.Unlock()
	if !changed {
		return
	}
	c.setCookie("validationKey", k)
	c.syncSession()
}

// syncSession copies the live cookies back into the account's session.
func (c *Client) syncSession() {
	if c.sess == nil {
		return
	}
	c.mu.Lock()
	vk := c.vk
	c.mu.Unlock()
	changed := false
	if vk != "" && c.sess.ValidationKey != vk {
		c.sess.ValidationKey, changed = vk, true
	}
	if j := c.cookie("JSESSIONID"); j != "" && j != c.sess.JSessionID {
		c.sess.JSessionID, changed = j, true
	}
	if changed && c.onChange != nil {
		c.onChange()
	}
}

func reqID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// Req is one SAPI request.
type Req struct {
	Method string
	Path   string // "/sapi/...?action=..."
	Base   string // host base; "" = c.Base
	Body   []byte
	Header map[string]string
	Upload bool // use the upload transport (no keep-alive, long header wait)
	Retry  bool // idempotent: retry transient failures
	NoKey  bool // don't append validationkey (public endpoints)
	Stream bool // don't read the body: Resp.Stream must be closed by the caller
	// BodyFn gives a fresh reader for every attempt (streamed uploads); size
	// is its exact length.
	BodyFn func() (io.Reader, error)
	Size   int64
}

type Resp struct {
	Status int
	Header http.Header
	Body   []byte
	Stream io.ReadCloser
}

func (c *Client) url(r Req) string {
	base := r.Base
	if base == "" {
		base = c.Base
	}
	u := base + r.Path
	if !r.NoKey {
		if k := c.key(); k != "" {
			sep := "?"
			if strings.Contains(u, "?") {
				sep = "&"
			}
			u += sep + "validationkey=" + url.QueryEscape(k)
		}
	}
	return u
}

// Do performs a request: adopts rotated validation keys (SEC-1003, up to 3
// times) and, when r.Retry, retries transient failures. A JSON
// {"error":...} body is an error even with HTTP 200.
func (c *Client) Do(ctx context.Context, r Req) (*Resp, error) {
	renewals, tries := 0, 0
	for {
		resp, err := c.once(ctx, r)
		if err == nil {
			return resp, nil
		}
		e := asErr(err)
		if e != nil && e.Kind == KindKeyRotated && renewals < 3 {
			renewals++
			log.Debugf("validation key rotada; se adopta (%d) y se repite", renewals)
			c.adoptKey(e.NewKey)
			continue
		}
		if r.Retry && e != nil && e.Kind == KindTransient && tries < 2 && ctx.Err() == nil {
			wait := []time.Duration{time.Second, 3 * time.Second}[tries]
			tries++
			log.Debugf("%s %s: %v; reintento %d en %s", r.Method, r.Path, err, tries, wait)
			select {
			case <-time.After(wait):
				continue
			case <-ctx.Done():
				return nil, err
			}
		}
		return nil, err
	}
}

func (c *Client) once(ctx context.Context, r Req) (*Resp, error) {
	var body io.Reader
	switch {
	case r.BodyFn != nil:
		rd, err := r.BodyFn()
		if err != nil {
			return nil, err
		}
		body = rd
	case r.Body != nil:
		body = bytes.NewReader(r.Body)
	}
	full := c.url(r)
	if !r.Stream && !r.Upload {
		// small API calls: never hang on a body that stops arriving
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, apiCallTimeout)
		defer cancel()
	}
	req, err := http.NewRequestWithContext(ctx, r.Method, full, body)
	if err != nil {
		return nil, err
	}
	if r.BodyFn != nil {
		req.ContentLength = r.Size
		if r.Size == 0 {
			req.Body = http.NoBody
		}
	}
	req.Header.Set("User-Agent", UserAgent)
	req.Header.Set("Accept", "application/json, text/plain, */*")
	req.Header.Set("X-deviceid", c.DeviceID)
	req.Header.Set("X-request-id", reqID())
	for k, v := range r.Header {
		req.Header[k] = []string{v} // keep the exact case the web/app use
	}
	op := r.Method + " " + strings.SplitN(r.Path, "&", 2)[0]
	log.Debugf("-> %s %s", r.Method, logx.Redact(full))
	hc := c.api
	if r.Upload {
		hc = c.upload
	}
	resp, err := hc.Do(req)
	if err != nil {
		log.Debugf("xx %s: %v", op, err)
		return nil, &Error{Op: op, Kind: KindTransient, Err: err}
	}
	c.afterResponse()
	if r.Stream && resp.StatusCode < 400 {
		log.Debugf("<- %s HTTP %d (stream)", op, resp.StatusCode)
		return &Resp{Status: resp.StatusCode, Header: resp.Header, Stream: resp.Body}, nil
	}
	b, rerr := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	resp.Body.Close()
	if rerr != nil {
		return nil, &Error{Op: op, Status: resp.StatusCode, Kind: KindTransient, Err: rerr}
	}
	log.Debugf("<- %s HTTP %d %s", op, resp.StatusCode, preview(b))
	if e := classify(op, resp.StatusCode, b); e != nil {
		return nil, e
	}
	c.absorbKey(b)
	return &Resp{Status: resp.StatusCode, Header: resp.Header, Body: b}, nil
}

func preview(b []byte) string {
	s := strings.Join(strings.Fields(string(b)), " ")
	if len(s) > 200 {
		s = s[:200] + "…"
	}
	return s
}

// afterResponse keeps the query-param key in sync with a validationKey cookie
// the server may have rotated via Set-Cookie.
func (c *Client) afterResponse() {
	if k := c.cookie("validationKey"); k != "" && k != c.key() {
		c.adoptKey(k)
	} else if j := c.cookie("JSESSIONID"); c.sess != nil && j != "" && j != c.sess.JSessionID {
		c.syncSession()
	}
}

// absorbKey: some answers carry data.validationkey.
func (c *Client) absorbKey(b []byte) {
	if !bytes.Contains(b, []byte("validationkey")) {
		return
	}
	var v struct {
		Data struct {
			ValidationKey string `json:"validationkey"`
		} `json:"data"`
	}
	if json.Unmarshal(b, &v) == nil && v.Data.ValidationKey != "" {
		c.adoptKey(v.Data.ValidationKey)
	}
}

type apiErr struct {
	Error *struct {
		Code    string          `json:"code"`
		Message string          `json:"message"`
		Data    json.RawMessage `json:"data"`
	} `json:"error"`
}

func classify(op string, status int, b []byte) error {
	var ae apiErr
	isJSON := json.Unmarshal(b, &ae) == nil
	if isJSON && ae.Error != nil {
		e := &Error{Op: op, Status: status, Code: ae.Error.Code, Msg: ae.Error.Message, Kind: KindAPI}
		var nk string
		_ = json.Unmarshal(ae.Error.Data, &nk)
		if nk != "" && (e.Code == "SEC-1003" ||
			strings.Contains(strings.ToLower(e.Msg), "validation key")) {
			e.Kind, e.NewKey = KindKeyRotated, nk
			return e
		}
		if status == 401 || status == 403 {
			e.Kind = KindSession
		} else if status == 408 || status == 429 || status >= 500 || e.Code == "PAPI-0000" {
			e.Kind = KindTransient
		}
		return mapCode(e)
	}
	switch {
	case status == 401 || status == 403:
		if len(bytes.TrimSpace(b)) == 0 {
			return &Error{Op: op, Status: status, Kind: KindEdge,
				Msg: "rechazo sin cuerpo (cortafuegos de O2 o X-deviceid de otra sesión)"}
		}
		return &Error{Op: op, Status: status, Kind: KindSession,
			Msg: "la sesión de O2 ha caducado o se ha invalidado"}
	case status == 404:
		return &Error{Op: op, Status: status, Kind: KindNotFound}
	case status == 408 || status == 429 || status >= 500:
		return &Error{Op: op, Status: status, Kind: KindTransient, Msg: preview(b)}
	case status >= 400:
		return &Error{Op: op, Status: status, Kind: KindAPI, Msg: preview(b)}
	}
	return nil
}

// data decodes {"data": ...} into v.
func data(r *Resp, v any) error {
	var w struct {
		Data json.RawMessage `json:"data"`
	}
	d := json.NewDecoder(bytes.NewReader(r.Body))
	d.UseNumber()
	if err := d.Decode(&w); err != nil {
		return fmt.Errorf("respuesta no JSON: %s", preview(r.Body))
	}
	if len(w.Data) == 0 {
		return errors.New("respuesta sin data")
	}
	d = json.NewDecoder(bytes.NewReader(w.Data))
	d.UseNumber()
	return d.Decode(v)
}

// fields decodes an answer that may or may not wrap its fields in "data"
// (upload answers are flat: {"success","id","status","etag"}).
func fields(r *Resp) map[string]any {
	var m map[string]any
	d := json.NewDecoder(bytes.NewReader(r.Body))
	d.UseNumber()
	if d.Decode(&m) != nil || m == nil {
		return map[string]any{}
	}
	if in, ok := m["data"].(map[string]any); ok {
		for k, v := range in {
			if _, dup := m[k]; !dup {
				m[k] = v
			}
		}
	}
	return m
}

// str/num read loosely-typed JSON values (O2 sends ids as numbers or strings).
func str(v any) string {
	switch x := v.(type) {
	case nil:
		return ""
	case string:
		return x
	case json.Number:
		return x.String()
	case float64:
		return fmt.Sprintf("%.0f", x)
	}
	return fmt.Sprint(v)
}

func num(v any) int64 {
	switch x := v.(type) {
	case json.Number:
		if n, err := x.Int64(); err == nil {
			return n
		}
		f, _ := x.Float64()
		return int64(f)
	case string:
		var n int64
		fmt.Sscan(x, &n)
		return n
	case float64:
		return int64(x)
	}
	return 0
}
