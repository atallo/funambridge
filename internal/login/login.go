// Package login does O2 Cloud's "normal" login (phone number + SMS), the same
// OAuth2 + PKCE flow the web uses since Nov 2026 (see docs/o2-sapi.md §3):
//
//  1. GET  {base}/                     -> JSESSIONID (O2 keeps the PKCE verifier)
//  2. GET  {base}/sapi/oauth/pkce/authorize?platform=web&deviceid=<id>
//     -> 302 apiseg.telefonica.es -> 302 t3.o2online.es/acceso/#/...?sessionID&sessionData
//  3. POST api.telefonica.es/.../manageCredentialMobileO2  -> O2 sends the SMS
//  4. POST .../verifyCredentialMobileO2                    -> {redirectUri}
//  5. GET  redirectUri (same jar)       -> Set-Cookie JSESSIONID + validationKey
//
// The session belongs to the deviceid of step 2 (uploads require it).
package login

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/atallo/funambridge/internal/config"
	"github.com/atallo/funambridge/internal/sapi"
)

const (
	T3API      = "https://api.telefonica.es/t3/cus/segu/v5/seguCredentialO2s/"
	consumerID = "O2CLOUD_WEB"
	cocoOrigin = "O2CLOUD_WEB/loginAppO2"
)

var ErrLogin = errors.New("login")

func fail(format string, a ...any) error {
	return fmt.Errorf("%w: %s", ErrLogin, fmt.Sprintf(format, a...))
}

var nonDigit = regexp.MustCompile(`\D`)

// NormalizePhone: "+34 600 11 22 33" / "0034…" / "600112233" -> "600112233".
func NormalizePhone(s string) (string, error) {
	d := nonDigit.ReplaceAllString(s, "")
	switch {
	case len(d) == 13 && strings.HasPrefix(d, "0034"):
		d = d[4:]
	case len(d) == 11 && strings.HasPrefix(d, "34"):
		d = d[2:]
	}
	if len(d) != 9 {
		return "", fail("el número debe tener 9 cifras")
	}
	return d, nil
}

// fragmentQuery parses "a=b&c=d" like the web's decodeURIComponent (a '+'
// stays a '+').
func fragmentQuery(s string) map[string]string {
	out := map[string]string{}
	for _, part := range strings.Split(s, "&") {
		k, v, ok := strings.Cut(part, "=")
		if !ok {
			continue
		}
		ku, _ := url.PathUnescape(k)
		vu, err := url.PathUnescape(v)
		if err != nil {
			vu = v
		}
		out[ku] = vu
	}
	return out
}

func uuid4() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

// Login is one login in progress: Start sends the SMS, Verify returns the
// session. Keep the same value between both steps.
type Login struct {
	Base     string
	DeviceID string
	Phone    string
	t3       string
	hc       *http.Client
	jar      *cookiejar.Jar
	gen      string
	sid      string
	sdata    string
	Started  time.Time
}

func New(base, deviceID string) *Login {
	if base == "" {
		base = config.DefaultBaseURL
	}
	if deviceID == "" {
		deviceID = config.NewDeviceID()
	}
	jar, _ := cookiejar.New(nil)
	return &Login{Base: strings.TrimRight(base, "/"), DeviceID: deviceID, t3: T3API, jar: jar,
		gen: uuid4(), Started: time.Now(),
		hc: &http.Client{Jar: jar, Timeout: 30 * time.Second, Transport: sapi.LoginTransport(),
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}
}

func (l *Login) get(ctx context.Context, u string) (*http.Response, error) {
	req, _ := http.NewRequestWithContext(ctx, "GET", u, nil)
	req.Header.Set("User-Agent", sapi.UserAgent)
	resp, err := l.hc.Do(req)
	if err != nil {
		return nil, fail("no se pudo conectar con %s: %v", hostOf(u), err)
	}
	io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
	resp.Body.Close()
	return resp, nil
}

func hostOf(u string) string {
	p, err := url.Parse(u)
	if err != nil {
		return u
	}
	return p.Host
}

func (l *Login) t3call(ctx context.Context, op string, payload any) (int, map[string]any, error) {
	b, _ := json.Marshal(payload)
	req, _ := http.NewRequestWithContext(ctx, "POST", l.t3+op, bytes.NewReader(b))
	for k, v := range map[string]string{
		"User-Agent": sapi.UserAgent, "Accept": "application/json, text/plain, */*",
		"Content-Type": "application/json", "Origin": "https://t3.o2online.es",
		"Referer": "https://t3.o2online.es/", "COCO.idOrigen": cocoOrigin,
		"COCO.idGeneral": l.gen, "COCO.idProceso": uuid4(), "COCO.idMensaje": uuid4()} {
		req.Header[k] = []string{v} // keep the exact header names
	}
	resp, err := l.hc.Do(req)
	if err != nil {
		return 0, nil, fail("no se pudo conectar con Telefónica: %v", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var d map[string]any
	_ = json.Unmarshal(raw, &d)
	return resp.StatusCode, d, nil
}

func errText(d map[string]any) string {
	for _, k := range []string{"message", "description", "error_description", "error", "code"} {
		if v, ok := d[k]; ok && fmt.Sprint(v) != "" {
			s := fmt.Sprint(v)
			if len(s) > 120 {
				s = s[:120]
			}
			return ": " + s
		}
	}
	return ""
}

// Start begins the login and makes O2 send the SMS to phone.
func (l *Login) Start(ctx context.Context, phone string) error {
	p, err := NormalizePhone(phone)
	if err != nil {
		return err
	}
	l.Phone = p
	if _, err := l.get(ctx, l.Base+"/"); err != nil {
		return err
	}
	resp, err := l.get(ctx, l.Base+"/sapi/oauth/pkce/authorize?platform=web&deviceid="+url.QueryEscape(l.DeviceID))
	if err != nil {
		return err
	}
	loc := resp.Header.Get("Location")
	st := resp.StatusCode
	for i := 0; i < 5 && loc != "" && !strings.Contains(loc, "#") && !strings.Contains(loc, "sessionData="); i++ {
		if resp, err = l.get(ctx, loc); err != nil {
			return err
		}
		st, loc = resp.StatusCode, resp.Header.Get("Location")
	}
	if loc == "" {
		return fail("O2 no inició el login (HTTP %d)", st)
	}
	frag := loc
	if _, f, ok := strings.Cut(loc, "#"); ok {
		frag = f
	} else if u, err := url.Parse(loc); err == nil {
		frag = u.RawQuery
	}
	if _, q, ok := strings.Cut(frag, "?"); ok {
		frag = q
	}
	q := fragmentQuery(frag)
	if q["sessionID"] == "" || q["sessionData"] == "" {
		return fail("no se obtuvo la sesión de login de Telefónica")
	}
	code, d, err := l.t3call(ctx, "manageCredentialMobileO2", map[string]string{
		"mobile": l.Phone, "sessionID": q["sessionID"], "sessionData": q["sessionData"], "consumerId": consumerID})
	if err != nil {
		return err
	}
	nd, _ := d["newSessionData"].(string)
	if code != 200 || nd == "" {
		return fail("O2 no envió el SMS (HTTP %d%s). ¿Es una línea de O2?", code, errText(d))
	}
	l.sdata = nd
	l.sid, _ = d["newSessionID"].(string)
	return nil
}

type Result struct {
	JSessionID, ValidationKey, DeviceID, BaseURL, Msisdn string
}

// Verify checks the SMS code and returns the new web session.
func (l *Login) Verify(ctx context.Context, code string) (*Result, error) {
	if l.sdata == "" {
		return nil, fail("primero hay que pedir el SMS")
	}
	otp := nonDigit.ReplaceAllString(code, "")
	if otp == "" {
		return nil, fail("falta el código del SMS")
	}
	st, d, err := l.t3call(ctx, "verifyCredentialMobileO2", map[string]string{
		"otp": otp, "sessionData": l.sdata, "sessionID": l.sid, "Mobile": l.Phone})
	if err != nil {
		return nil, err
	}
	redirect, _ := d["redirectUri"].(string)
	if st != 200 || !strings.HasPrefix(redirect, l.Base+"/sapi/login/oauth") {
		return nil, fail("código no aceptado (HTTP %d%s)", st, errText(d))
	}
	if _, err := l.get(ctx, redirect); err != nil {
		return nil, err
	}
	var js, vk string
	u, _ := url.Parse(l.Base + "/")
	for _, c := range l.jar.Cookies(u) {
		switch c.Name {
		case "JSESSIONID":
			js = c.Value
		case "validationKey":
			vk = c.Value
		}
	}
	if js == "" || vk == "" {
		return nil, fail("O2 no creó la sesión tras validar el código")
	}
	return &Result{JSessionID: js, ValidationKey: vk, DeviceID: l.DeviceID, BaseURL: l.Base,
		Msisdn: "+34" + l.Phone}, nil
}
