// Package logx is a tiny leveled logger: INFO and above to stderr, everything
// (DEBUG included) to an optional rotating file, and the last lines kept in
// memory for the admin panel's live log.
package logx

import (
	"fmt"
	"io"
	"os"
	"regexp"
	"sync"
	"time"
)

type Level int

const (
	Debug Level = iota
	Info
	Warn
	Error
)

func (l Level) String() string {
	return [...]string{"DEBUG", "INFO", "WARNING", "ERROR"}[l]
}

type Line struct {
	N     int64  `json:"n"`
	Time  string `json:"t"`
	Level string `json:"level"`
	Text  string `json:"text"`
}

var (
	mu        sync.Mutex
	console   io.Writer = os.Stderr
	consoleLv           = Info
	fileW     *rotating
	ring      = make([]Line, 0, ringSize)
	ringStart int
	seq       int64
	hooks     []func(Level, string)
)

const ringSize = 800

// secrets that must never reach a log line
var redactRe = []*regexp.Regexp{
	regexp.MustCompile(`(?i)(validationkey=)[^&\s"]+`),
	regexp.MustCompile(`([?&]k=)[^&\s"]+`),
	regexp.MustCompile(`(?i)(jsessionid=)[^;&\s"]+`),
	regexp.MustCompile(`(/bot)[0-9]+:[A-Za-z0-9_-]+`),
}

func Redact(s string) string {
	for _, re := range redactRe {
		s = re.ReplaceAllString(s, "${1}<redacted>")
	}
	return s
}

// SetVerbose shows DEBUG on the console too.
func SetVerbose(v bool) {
	mu.Lock()
	defer mu.Unlock()
	if v {
		consoleLv = Debug
	} else {
		consoleLv = Info
	}
}

// SetFile adds a rotating DEBUG log file (5 MB x 3, like the Python version).
func SetFile(path string) error {
	r, err := newRotating(path, 5_000_000, 3)
	if err != nil {
		return err
	}
	mu.Lock()
	fileW = r
	mu.Unlock()
	return nil
}

// OnLog registers a hook called for WARNING and ERROR lines (alerts).
func OnLog(f func(Level, string)) {
	mu.Lock()
	hooks = append(hooks, f)
	mu.Unlock()
}

func logf(l Level, comp, format string, a ...any) {
	text := Redact(fmt.Sprintf(format, a...))
	if comp != "" {
		text = comp + ": " + text
	}
	now := time.Now()
	stamp := now.Format("2006-01-02 15:04:05.000")
	line := fmt.Sprintf("%s %s %s\n", stamp, l, text)
	mu.Lock()
	if l >= consoleLv {
		_, _ = io.WriteString(console, line)
	}
	if fileW != nil {
		_, _ = fileW.Write([]byte(line))
	}
	if l >= Info {
		seq++
		ln := Line{N: seq, Time: now.Format("02/01 15:04:05"), Level: l.String(), Text: text}
		if len(ring) < ringSize {
			ring = append(ring, ln)
		} else {
			ring[ringStart] = ln
			ringStart = (ringStart + 1) % ringSize
		}
	}
	hs := hooks
	mu.Unlock()
	if l >= Warn {
		for _, h := range hs {
			h(l, text)
		}
	}
}

// Since returns the kept lines with N > after (oldest first).
func Since(after int64) []Line {
	mu.Lock()
	defer mu.Unlock()
	out := []Line{}
	for i := 0; i < len(ring); i++ {
		ln := ring[(ringStart+i)%len(ring)]
		if ln.N > after {
			out = append(out, ln)
		}
	}
	return out
}

// Logger is a component-tagged logger ("sapi", "webdav", ...).
type Logger struct{ comp string }

func New(component string) Logger { return Logger{component} }

func (g Logger) Debugf(f string, a ...any) { logf(Debug, g.comp, f, a...) }
func (g Logger) Infof(f string, a ...any)  { logf(Info, g.comp, f, a...) }
func (g Logger) Warnf(f string, a ...any)  { logf(Warn, g.comp, f, a...) }
func (g Logger) Errorf(f string, a ...any) { logf(Error, g.comp, f, a...) }

// rotating is a size-based rotating file (path, path.1 .. path.N).
type rotating struct {
	path string
	max  int64
	keep int
	f    *os.File
	size int64
}

func newRotating(path string, max int64, keep int) (*rotating, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, err
	}
	st, _ := f.Stat()
	var sz int64
	if st != nil {
		sz = st.Size()
	}
	return &rotating{path: path, max: max, keep: keep, f: f, size: sz}, nil
}

func (r *rotating) Write(p []byte) (int, error) {
	if r.size+int64(len(p)) > r.max {
		r.rotate()
	}
	n, err := r.f.Write(p)
	r.size += int64(n)
	return n, err
}

func (r *rotating) rotate() {
	_ = r.f.Close()
	for i := r.keep - 1; i >= 1; i-- {
		_ = os.Rename(fmt.Sprintf("%s.%d", r.path, i), fmt.Sprintf("%s.%d", r.path, i+1))
	}
	_ = os.Rename(r.path, r.path+".1")
	f, err := os.OpenFile(r.path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
	if err == nil {
		r.f, r.size = f, 0
	}
}
