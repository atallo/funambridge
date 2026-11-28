package server

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"

	"golang.org/x/net/webdav"

	"github.com/atallo/funambridge/internal/config"
	"github.com/atallo/funambridge/internal/davfs"
	"github.com/atallo/funambridge/internal/login"
	"github.com/atallo/funambridge/internal/logx"
	"github.com/atallo/funambridge/internal/notify"
	"github.com/atallo/funambridge/internal/sapi"
	"github.com/atallo/funambridge/internal/store"
	"github.com/atallo/funambridge/internal/transfers"
)

var log = logx.New("http")
var klog = logx.New("keepalive")

// Live is the runtime side of a configured account.
type Live struct {
	Acc   *config.Account
	Store *store.Store
	FS    *davfs.FS
	locks webdav.LockSystem
	dav   map[string]*webdav.Handler // by URL prefix ("" or "/dav")
}

type aliveState struct {
	session string
	ok      time.Time
	fail    time.Time
	since   time.Time
}

type pending struct {
	l     *login.Login
	name  string
	start time.Time
}

type Registry struct {
	Cfg *config.Config
	Tr  *transfers.Tracker
	TG  *notify.Telegram

	mu      sync.Mutex
	live    map[string]*Live // by access key
	alive   map[string]*aliveState
	pending map[string]*pending
	stop    chan struct{}
}

func NewRegistry(cfg *config.Config) *Registry {
	r := &Registry{Cfg: cfg, Tr: transfers.New(), TG: notify.New(), live: map[string]*Live{},
		alive: map[string]*aliveState{}, pending: map[string]*pending{}, stop: make(chan struct{})}
	r.Tr.SetLimits(cfg.MaxUploads, cfg.MaxDownloads)
	r.TG.Configure(cfg.Telegram.BotToken, cfg.Telegram.ChatID)
	return r
}

// baseDir holds the PUT spool and the write cache ("cache_dir", else the
// system temp dir).
func (r *Registry) baseDir() string {
	d := r.Cfg.CacheDir
	if d == "" {
		d = filepath.Join(os.TempDir(), "funambridge-cache")
	}
	return d
}

func (r *Registry) spoolDir() string {
	d := filepath.Join(r.baseDir(), "spool")
	_ = os.MkdirAll(d, 0o700)
	return d
}

func (r *Registry) save() {
	if err := r.Cfg.Save(); err != nil {
		log.Errorf("no se pudo guardar %s: %v", r.Cfg.Path, err)
	}
}

// For returns (building it on first use) the runtime of an account.
func (r *Registry) For(a *config.Account) *Live {
	r.mu.Lock()
	defer r.mu.Unlock()
	if l, ok := r.live[a.AccessKey]; ok && l.Acc == a {
		return l
	}
	c := sapi.New(a.BaseURL, a.DeviceID, &a.Session, func() { go r.save() })
	cache := store.NewCache(a.CacheSeconds, filepath.Join(r.baseDir(), a.AccessKey))
	st := store.New(c, a.Name, r.Tr, r.spoolDir(), cache)
	l := &Live{Acc: a, Store: st, FS: davfs.New(st), locks: webdav.NewMemLS(),
		dav: map[string]*webdav.Handler{}}
	r.live[a.AccessKey] = l
	return l
}

func (l *Live) handler(prefix string) *webdav.Handler {
	if h, ok := l.dav[prefix]; ok {
		return h
	}
	h := &webdav.Handler{Prefix: prefix, FileSystem: l.FS, LockSystem: l.locks,
		Logger: func(req *http.Request, err error) {
			if err != nil {
				log.Debugf("%s %s: %v", req.Method, req.URL.Path, err)
			}
		}}
	l.dav[prefix] = h
	return h
}

// Drop forgets the runtime of an account (after its session or keys changed).
func (r *Registry) Drop(accessKey string) {
	r.mu.Lock()
	l := r.live[accessKey]
	delete(r.live, accessKey)
	r.mu.Unlock()
	if l != nil {
		l.Store.Cache.Close() // stop its sweeper, remove its files
	}
}

// CacheOverview lists the write-cache entries of every live account.
func (r *Registry) CacheOverview() []store.CacheView {
	r.mu.Lock()
	lives := make([]*Live, 0, len(r.live))
	for _, l := range r.live {
		lives = append(lives, l)
	}
	r.mu.Unlock()
	out := []store.CacheView{}
	for _, l := range lives {
		out = append(out, l.Store.Cache.Snapshot(l.Acc.Name)...)
	}
	return out
}

// ---- keepalive ---------------------------------------------------------------

// Since Nov 2026 O2 ends a web session after ~1-2 h without requests; one
// cheap authenticated call every few minutes keeps it alive between backups.
func (r *Registry) StartKeepalive() {
	mins := r.Cfg.KeepaliveMinutes
	if mins <= 0 {
		klog.Infof("keepalive desactivado (keepalive_minutes: 0)")
		return
	}
	klog.Infof("keepalive: ping a O2 cada %d min por cuenta", mins)
	go func() {
		t := time.NewTicker(time.Duration(mins) * time.Minute)
		defer t.Stop()
		for {
			select {
			case <-r.stop:
				return
			case <-t.C:
				for _, a := range r.Cfg.Accounts {
					r.Ping(a)
				}
			}
		}
	}()
}

func (r *Registry) aliveFor(a *config.Account) *aliveState {
	st := r.alive[a.AccessKey]
	if st == nil || st.session != a.Session.JSessionID {
		st = &aliveState{session: a.Session.JSessionID, since: time.Now()}
		r.alive[a.AccessKey] = st
	}
	return st
}

// Ping checks/keeps alive an account's session. Stops pinging a session O2
// already rejected, until it is renewed.
func (r *Registry) Ping(a *config.Account) {
	if !a.HasAuth() {
		return
	}
	r.mu.Lock()
	st := r.aliveFor(a)
	dead := !st.fail.IsZero()
	r.mu.Unlock()
	if dead {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	err := r.For(a).Store.C.Profile(ctx)
	switch {
	case err == nil:
		r.mu.Lock()
		st.ok = time.Now()
		r.mu.Unlock()
		klog.Debugf("%s: sesión viva", a.Name)
	case sapi.IsSession(err):
		r.SessionDead(a, err)
	default:
		klog.Debugf("%s: %v (se reintenta en la próxima ronda)", a.Name, err)
	}
}

// SessionDead records that O2 rejected an account's session and alerts.
func (r *Registry) SessionDead(a *config.Account, err error) {
	r.mu.Lock()
	st := r.aliveFor(a)
	first := st.fail.IsZero()
	if first {
		st.fail = time.Now()
	}
	lived := time.Since(st.since).Hours()
	r.mu.Unlock()
	if !first {
		return
	}
	klog.Warnf("la sesión de O2 de '%s' ha caducado (viva %.1f h): renuévala desde el panel (%v)",
		a.Name, lived, err)
	r.TG.Alert("session:"+a.Name, "la sesión de O2 de '"+a.Name+
		"' ha caducado. Renuévala en el panel («Renovar sesión» → SMS); hasta entonces las copias fallan.")
}

type AliveView struct {
	OK   time.Time
	Fail time.Time
}

func (r *Registry) Alive(a *config.Account) AliveView {
	r.mu.Lock()
	defer r.mu.Unlock()
	st := r.alive[a.AccessKey]
	if st == nil || st.session != a.Session.JSessionID {
		return AliveView{}
	}
	return AliveView{OK: st.ok, Fail: st.fail}
}

// ---- SMS logins waiting for their code ----------------------------------------

const pendingTTL = 10 * time.Minute

func token() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func (r *Registry) addPending(l *login.Login, name string) string {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.purge()
	t := token()
	r.pending[t] = &pending{l: l, name: name, start: time.Now()}
	return t
}

func (r *Registry) getPending(t string) *pending {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.purge()
	return r.pending[t]
}

func (r *Registry) popPending(t string) {
	r.mu.Lock()
	delete(r.pending, t)
	r.mu.Unlock()
}

func (r *Registry) purge() {
	for k, p := range r.pending {
		if time.Since(p.start) > pendingTTL {
			delete(r.pending, k)
		}
	}
}
