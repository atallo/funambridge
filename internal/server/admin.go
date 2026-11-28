package server

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"html/template"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/atallo/funambridge/internal/config"
	"github.com/atallo/funambridge/internal/har"
	"github.com/atallo/funambridge/internal/login"
	"github.com/atallo/funambridge/internal/logx"
	"github.com/atallo/funambridge/internal/sapi"
	"github.com/atallo/funambridge/internal/version"
)

const o2Web = "https://cloud.o2online.es"

//go:embed panel.html
var panelHTML string

var panelTmpl = template.Must(template.New("panel").Parse(panelHTML))

type accView struct {
	I         int
	Name      string
	Phone     string
	PhoneDig  string
	User      string
	Pass      string
	HasAuth   bool
	ReadOnly  bool
	AliveOK   string
	AliveFail string
}

type pageData struct {
	Version     string
	Fingerprint string
	O2Web       string
	Admin       string
	Msg, Err    string
	Accounts    []accView
	Pending     *struct{ Token, Name, Masked string }
	MaxUp       int
	MaxDown     int
	Keepalive   int
	TGOn        bool
	TGChat      string
}

func (s *Server) admin(w http.ResponseWriter, r *http.Request) {
	p := strings.TrimPrefix(r.URL.Path, AdminPrefix)
	if r.Method == http.MethodPost {
		s.adminPost(w, r, p)
		return
	}
	switch p {
	case "", "/":
		s.page(w, r)
	case "/browse.json":
		s.browse(w, r)
	case "/transfers.json":
		writeJSON(w, s.R.Tr.Snapshot())
	case "/logs.json":
		after, _ := strconv.ParseInt(r.URL.Query().Get("after"), 10, 64)
		writeJSON(w, logx.Since(after))
	default:
		http.NotFound(w, r)
	}
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_ = json.NewEncoder(w).Encode(v)
}

func hm(t time.Time) string { return t.Format("02/01 15:04") }

func (s *Server) page(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	cfg := s.R.Cfg
	d := pageData{Version: version.Version, Fingerprint: sapi.FingerprintName(), O2Web: o2Web,
		Admin: AdminPrefix, Msg: q.Get("msg"), Err: q.Get("err"), MaxUp: cfg.MaxUploads,
		MaxDown: cfg.MaxDownloads, Keepalive: cfg.KeepaliveMinutes, TGOn: cfg.Telegram.Enabled(),
		TGChat: cfg.Telegram.ChatID}
	if t := q.Get("login"); t != "" {
		if pl := s.R.getPending(t); pl != nil {
			d.Pending = &struct{ Token, Name, Masked string }{t, pl.name, "···" + pl.l.Phone[len(pl.l.Phone)-3:]}
		} else if d.Err == "" {
			d.Err = "Ese inicio de sesión ha caducado: vuelve a pedir el SMS"
		}
	}
	for i, a := range cfg.Accounts {
		v := accView{I: i, Name: a.Name, Phone: a.Phone(), User: a.AccessKey, Pass: a.SecretKey,
			HasAuth: a.HasAuth(), ReadOnly: a.HasAuth() && !strings.HasPrefix(a.DeviceID, "web-")}
		v.PhoneDig = strings.TrimPrefix(a.Phone(), "+34")
		al := s.R.Alive(a)
		if !al.Fail.IsZero() {
			v.AliveFail = hm(al.Fail)
		} else if !al.OK.IsZero() {
			v.AliveOK = hm(al.OK)
		}
		d.Accounts = append(d.Accounts, v)
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	if err := panelTmpl.Execute(w, d); err != nil {
		log.Errorf("panel: %v", err)
	}
}

// browse: GET /_admin/browse.json?name=&path= -- checks the session with a real
// call and lists a folder FRESH from O2 (no index), so a good answer proves the
// credentials work.
func (s *Server) browse(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	out := func(m map[string]any) { writeJSON(w, m) }
	a := s.R.Cfg.ByName(q.Get("name"))
	if a == nil {
		out(map[string]any{"ok": false, "kind": "noaccount", "error": "cuenta no encontrada"})
		return
	}
	if !a.HasAuth() {
		out(map[string]any{"ok": false, "kind": "noauth", "error": "la cuenta no tiene sesión guardada"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()
	live := s.R.For(a)
	c := live.Store.C
	if err := c.Profile(ctx); err != nil {
		kind := "upstream"
		if sapi.IsSession(err) {
			kind = "session"
			s.R.SessionDead(a, err)
		}
		out(map[string]any{"ok": false, "kind": kind, "error": err.Error()})
		return
	}
	pth := strings.Trim(q.Get("path"), "/")
	live.Store.InvalidateTree()
	fid, err := live.Store.Dir(ctx, pth, false)
	if err != nil {
		out(map[string]any{"ok": false, "kind": "upstream", "error": err.Error()})
		return
	}
	if fid == "" {
		out(map[string]any{"ok": false, "kind": "notfound", "error": "carpeta no encontrada: /" + pth})
		return
	}
	subs, _ := live.Store.SubDirs(ctx, fid)
	ms, err := c.ListMedia(ctx, fid)
	if err != nil {
		out(map[string]any{"ok": false, "kind": "upstream", "error": err.Error()})
		return
	}
	sort.Slice(ms, func(i, j int) bool { return strings.ToLower(ms[i].Name) < strings.ToLower(ms[j].Name) })
	files := []map[string]any{}
	for i, m := range ms {
		if i >= 500 {
			break
		}
		files = append(files, map[string]any{"name": m.Name, "size": m.Size, "modified": m.Modified})
	}
	res := map[string]any{"ok": true, "path": pth, "folders": subs, "files": files,
		"total_files": len(ms), "checked": time.Now().Format("15:04:05"), "limits": c.Limits(ctx),
		"fingerprint": sapi.FingerprintName()}
	if st, err := c.Storage(ctx); err == nil {
		res["storage"] = st
	}
	out(res)
}

func (s *Server) back(w http.ResponseWriter, r *http.Request, msg, errMsg string, extra ...string) {
	v := url.Values{}
	if msg != "" {
		v.Set("msg", msg)
	}
	if errMsg != "" {
		v.Set("err", errMsg)
	}
	for i := 0; i+1 < len(extra); i += 2 {
		v.Set(extra[i], extra[i+1])
	}
	u := AdminPrefix + "/"
	if len(v) > 0 {
		u += "?" + v.Encode()
	}
	http.Redirect(w, r, u, http.StatusSeeOther)
}

func (s *Server) adminPost(w http.ResponseWriter, r *http.Request, p string) {
	cfg := s.R.Cfg
	if p == "/import-har" {
		if err := r.ParseMultipartForm(64 << 20); err != nil {
			s.back(w, r, "", "HAR: "+err.Error())
			return
		}
		f, _, err := r.FormFile("har")
		if err != nil {
			s.back(w, r, "", "HAR: falta el fichero")
			return
		}
		b, _ := io.ReadAll(io.LimitReader(f, 512<<20))
		f.Close()
		hs := har.Parse(string(b))
		if hs.JSessionID == "" || hs.ValidationKey == "" {
			s.back(w, r, "", "HAR: no contiene una sesión iniciada en cloud.o2online.es (¿exportado sin datos sensibles?)")
			return
		}
		name := strings.TrimSpace(r.FormValue("name"))
		if name == "" {
			name = "o2"
			if hs.Msisdn != "" {
				name = "o2-" + hs.Msisdn
			}
		}
		cfg.Lock()
		a, err := cfg.UpsertSession(name, hs.JSessionID, hs.ValidationKey, hs.PLC, hs.BaseURL, hs.DeviceID)
		if err == nil && hs.Msisdn != "" {
			a.OAuth.Msisdn = hs.Msisdn
		}
		cfg.Unlock()
		if err != nil {
			s.back(w, r, "", "HAR: "+err.Error())
			return
		}
		s.R.save()
		s.R.Drop(a.AccessKey)
		s.back(w, r, "Sesión importada para '"+a.Name+"'", "")
		return
	}
	if err := r.ParseForm(); err != nil {
		s.back(w, r, "", err.Error())
		return
	}
	f := r.PostFormValue
	switch p {
	case "/login-start":
		name := strings.TrimSpace(f("name"))
		if name == "" {
			name = "o2"
		}
		base, dev := config.DefaultBaseURL, ""
		if a := cfg.ByName(name); a != nil {
			base = a.BaseURL
			if strings.HasPrefix(a.DeviceID, "web-") {
				dev = a.DeviceID // keep a stable device per account
			}
		}
		lg := login.New(base, dev)
		ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
		defer cancel()
		if err := lg.Start(ctx, f("phone")); err != nil {
			s.back(w, r, "", "SMS: "+strings.TrimPrefix(err.Error(), "login: "))
			return
		}
		t := s.R.addPending(lg, name)
		s.back(w, r, "SMS enviado a ···"+lg.Phone[len(lg.Phone)-3:]+": escribe el código", "", "login", t)
	case "/login-verify":
		t := f("token")
		pl := s.R.getPending(t)
		if pl == nil {
			s.back(w, r, "", "Ese inicio de sesión ha caducado: vuelve a pedir el SMS")
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
		defer cancel()
		res, err := pl.l.Verify(ctx, f("code"))
		if err != nil {
			s.back(w, r, "", strings.TrimPrefix(err.Error(), "login: "), "login", t)
			return
		}
		s.R.popPending(t)
		cfg.Lock()
		a, err := cfg.UpsertSession(pl.name, res.JSessionID, res.ValidationKey, "", res.BaseURL, res.DeviceID)
		if err == nil {
			a.OAuth.Msisdn = res.Msisdn
		}
		cfg.Unlock()
		if err != nil {
			s.back(w, r, "", err.Error())
			return
		}
		s.R.save()
		s.R.Drop(a.AccessKey)
		logx.New("login").Infof("sesión iniciada por SMS para '%s'", a.Name)
		s.back(w, r, "Sesión iniciada para '"+a.Name+"'", "")
	case "/login-cancel":
		s.R.popPending(f("token"))
		s.back(w, r, "", "")
	case "/set-session":
		cfg.Lock()
		a, err := cfg.UpsertSession(strings.TrimSpace(f("name")), f("jsessionid"), f("validationkey"),
			f("plc"), "", f("deviceid"))
		cfg.Unlock()
		if err != nil {
			s.back(w, r, "", err.Error())
			return
		}
		s.R.save()
		s.R.Drop(a.AccessKey)
		s.back(w, r, "Sesión guardada para '"+a.Name+"'", "")
	case "/remove":
		cfg.Lock()
		if a := cfg.ByName(f("name")); a != nil {
			s.R.Drop(a.AccessKey)
		}
		cfg.RemoveAccount(f("name"))
		cfg.Unlock()
		s.R.save()
		s.back(w, r, "Cuenta eliminada", "")
	case "/keys":
		cfg.Lock()
		a := cfg.ByName(f("name"))
		if a == nil {
			cfg.Unlock()
			s.back(w, r, "", "cuenta no encontrada")
			return
		}
		old := a.AccessKey
		u, pw := strings.TrimSpace(f("access_key")), strings.TrimSpace(f("secret_key"))
		if f("mode") != "set" || u == "" {
			u = config.NewUser()
		}
		if f("mode") != "set" || pw == "" {
			pw = config.NewPassword()
		}
		a.AccessKey, a.SecretKey = u, pw
		cfg.Unlock()
		s.R.save()
		s.R.Drop(old)
		s.back(w, r, "Usuario y contraseña actualizados para '"+a.Name+"'", "")
	case "/settings":
		up, _ := strconv.Atoi(f("max_uploads"))
		down, _ := strconv.Atoi(f("max_downloads"))
		ka, err := strconv.Atoi(f("keepalive"))
		if err != nil || up < 0 || down < 0 || ka < 0 {
			s.back(w, r, "", "valores no válidos")
			return
		}
		cfg.Lock()
		cfg.MaxUploads, cfg.MaxDownloads, cfg.KeepaliveMinutes = up, down, ka
		cfg.Unlock()
		s.R.Tr.SetLimits(up, down)
		s.R.save()
		s.back(w, r, "Ajustes guardados (el keepalive nuevo se aplica al reiniciar)", "")
	case "/telegram":
		tok, chat := strings.TrimSpace(f("bot_token")), strings.TrimSpace(f("chat_id"))
		cfg.Lock()
		if f("mode") == "off" {
			cfg.Telegram = config.Telegram{}
		} else {
			if tok != "" {
				cfg.Telegram.BotToken = tok
			}
			if chat != "" {
				cfg.Telegram.ChatID = chat
			}
		}
		t := cfg.Telegram
		cfg.Unlock()
		s.R.TG.Configure(t.BotToken, t.ChatID)
		s.R.save()
		if f("mode") == "off" {
			s.back(w, r, "Avisos de Telegram desactivados", "")
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
		defer cancel()
		if err := s.R.TG.Send(ctx, "avisos activados ✔"); err != nil {
			s.back(w, r, "", "Telegram guardado, pero la prueba falló: "+err.Error())
			return
		}
		s.back(w, r, "Telegram configurado: te ha llegado un mensaje de prueba", "")
	default:
		http.NotFound(w, r)
	}
}

var _ = errors.New
