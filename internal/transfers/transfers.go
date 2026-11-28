// Package transfers tracks uploads/downloads in flight (for the panel) and
// optionally limits how many run at once against O2.
package transfers

import (
	"context"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

type Kind string

const (
	Up   Kind = "subida"
	Down Kind = "descarga"
)

type T struct {
	ID      int64
	Kind    Kind
	Account string
	Path    string
	Size    int64
	done    atomic.Int64
	start   time.Time
	phase   atomic.Value // string
	end     time.Time
	result  string // "" while running; "ok" or the error
}

func (t *T) SetDone(n int64)   { t.done.Store(n) }
func (t *T) AddDone(n int64)   { t.done.Add(n) }
func (t *T) SetPhase(p string) { t.phase.Store(p) }
func (t *T) Phase() string     { s, _ := t.phase.Load().(string); return s }

// View is the JSON shape for the panel.
type View struct {
	ID      int64   `json:"id"`
	Kind    Kind    `json:"kind"`
	Account string  `json:"account"`
	Path    string  `json:"path"`
	Size    int64   `json:"size"`
	Done    int64   `json:"done"`
	Phase   string  `json:"phase"`
	Seconds float64 `json:"seconds"`
	Speed   float64 `json:"speed"` // bytes/s
	Result  string  `json:"result,omitempty"`
	Ended   string  `json:"ended,omitempty"`
}

type Tracker struct {
	mu      sync.Mutex
	next    int64
	active  map[int64]*T
	recent  []View // finished, newest first
	upSem   *sem
	downSem *sem
	totals  struct{ upOK, upFail, downOK, downFail int64 }
}

func New() *Tracker { return &Tracker{active: map[int64]*T{}, upSem: newSem(0), downSem: newSem(0)} }

// SetLimits changes the parallelism (0 = unlimited) on the fly.
func (tr *Tracker) SetLimits(up, down int) {
	tr.upSem.resize(up)
	tr.downSem.resize(down)
}

func (tr *Tracker) Start(k Kind, account, path string, size int64) *T {
	tr.mu.Lock()
	defer tr.mu.Unlock()
	tr.next++
	t := &T{ID: tr.next, Kind: k, Account: account, Path: path, Size: size, start: time.Now()}
	t.SetPhase("preparando")
	tr.active[t.ID] = t
	return t
}

// Acquire waits for a free slot of t's kind (no-op when unlimited). The
// returned func releases it.
func (tr *Tracker) Acquire(ctx context.Context, t *T) (func(), error) {
	s := tr.upSem
	if t.Kind == Down {
		s = tr.downSem
	}
	if !s.limited() {
		return func() {}, nil
	}
	prev := t.Phase()
	t.SetPhase("en cola")
	if err := s.acquire(ctx); err != nil {
		return nil, err
	}
	t.SetPhase(prev)
	return s.release, nil
}

func (tr *Tracker) Finish(t *T, err error) {
	tr.mu.Lock()
	defer tr.mu.Unlock()
	delete(tr.active, t.ID)
	t.end = time.Now()
	if err != nil {
		t.result = err.Error()
	} else {
		t.result = "ok"
	}
	switch {
	case t.Kind == Up && err == nil:
		tr.totals.upOK++
	case t.Kind == Up:
		tr.totals.upFail++
	case err == nil:
		tr.totals.downOK++
	default:
		tr.totals.downFail++
	}
	v := view(t, t.end)
	v.Ended = t.end.Format("15:04:05")
	tr.recent = append([]View{v}, tr.recent...)
	if len(tr.recent) > 60 {
		tr.recent = tr.recent[:60]
	}
}

func view(t *T, now time.Time) View {
	el := now.Sub(t.start).Seconds()
	d := t.done.Load()
	v := View{ID: t.ID, Kind: t.Kind, Account: t.Account, Path: t.Path, Size: t.Size, Done: d,
		Phase: t.Phase(), Seconds: el, Result: t.result}
	if el > 0.2 {
		v.Speed = float64(d) / el
	}
	return v
}

type Snapshot struct {
	Active   []View `json:"active"`
	Recent   []View `json:"recent"`
	UpOK     int64  `json:"up_ok"`
	UpFail   int64  `json:"up_fail"`
	DownOK   int64  `json:"down_ok"`
	DownFail int64  `json:"down_fail"`
	MaxUp    int    `json:"max_up"`
	MaxDown  int    `json:"max_down"`
}

func (tr *Tracker) Snapshot() Snapshot {
	tr.mu.Lock()
	defer tr.mu.Unlock()
	now := time.Now()
	s := Snapshot{Recent: append([]View(nil), tr.recent...), UpOK: tr.totals.upOK,
		UpFail: tr.totals.upFail, DownOK: tr.totals.downOK, DownFail: tr.totals.downFail,
		MaxUp: tr.upSem.size(), MaxDown: tr.downSem.size()}
	for _, t := range tr.active {
		s.Active = append(s.Active, view(t, now))
	}
	sort.Slice(s.Active, func(i, j int) bool { return s.Active[i].ID < s.Active[j].ID })
	return s
}

// sem is a resizable counting semaphore; n <= 0 means unlimited.
type sem struct {
	mu   sync.Mutex
	cond *sync.Cond
	n    int
	used int
}

func newSem(n int) *sem { s := &sem{n: n}; s.cond = sync.NewCond(&s.mu); return s }

func (s *sem) limited() bool { s.mu.Lock(); defer s.mu.Unlock(); return s.n > 0 }
func (s *sem) size() int     { s.mu.Lock(); defer s.mu.Unlock(); return s.n }

func (s *sem) resize(n int) {
	s.mu.Lock()
	s.n = n
	s.mu.Unlock()
	s.cond.Broadcast()
}

func (s *sem) acquire(ctx context.Context) error {
	stop := context.AfterFunc(ctx, func() { s.cond.Broadcast() })
	defer stop()
	s.mu.Lock()
	defer s.mu.Unlock()
	for s.n > 0 && s.used >= s.n {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		s.cond.Wait()
	}
	s.used++
	return nil
}

func (s *sem) release() {
	s.mu.Lock()
	if s.used > 0 {
		s.used--
	}
	s.mu.Unlock()
	s.cond.Signal()
}
