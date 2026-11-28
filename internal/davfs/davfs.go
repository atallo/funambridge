// Package davfs adapts a store.Store to golang.org/x/net/webdav.FileSystem.
//
// O2 errors are recorded in the request context (Note/ErrOf) so the HTTP layer
// can answer what they mean (503 + Retry-After, 413, 415...) instead of the
// generic 405/500 x/net/webdav would send.
package davfs

import (
	"context"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"mime"
	"os"
	"path"
	"strings"
	"sync"
	"time"

	"golang.org/x/net/webdav"

	"github.com/atallo/funambridge/internal/logx"
	"github.com/atallo/funambridge/internal/sapi"
	"github.com/atallo/funambridge/internal/store"
	"github.com/atallo/funambridge/internal/transfers"
)

var log = logx.New("webdav")

// ---- per-request error holder ----------------------------------------------

type holderKey struct{}

type holder struct {
	mu  sync.Mutex
	err error
}

func WithHolder(ctx context.Context) context.Context {
	return context.WithValue(ctx, holderKey{}, &holder{})
}

// Note records the O2 error behind a failed operation and returns err.
func Note(ctx context.Context, err error) error {
	if h, ok := ctx.Value(holderKey{}).(*holder); ok && err != nil {
		h.mu.Lock()
		if h.err == nil {
			h.err = err
		}
		h.mu.Unlock()
	}
	return err
}

func ErrOf(ctx context.Context) error {
	if h, ok := ctx.Value(holderKey{}).(*holder); ok {
		h.mu.Lock()
		defer h.mu.Unlock()
		return h.err
	}
	return nil
}

// ---- modification time sent by the client (X-OC-Mtime) ------------------------

type mtimeKey struct{}

func WithMtime(ctx context.Context, t time.Time) context.Context {
	return context.WithValue(ctx, mtimeKey{}, t)
}

func mtimeOf(ctx context.Context) time.Time {
	t, _ := ctx.Value(mtimeKey{}).(time.Time)
	return t
}

// ---- OS junk: absorbed, never uploaded ------------------------------------------

// IsJunk: files operating systems drop everywhere (.DS_Store, Thumbs.db...).
func IsJunk(p string) bool {
	n := strings.ToLower(path.Base(p))
	switch n {
	case ".ds_store", "desktop.ini", "thumbs.db", "ehthumbs.db", ".localized", "icon\r":
		return true
	}
	if strings.HasPrefix(n, "._") || strings.HasPrefix(n, ".trash-") || n == "$recycle.bin" ||
		strings.HasPrefix(n, ".~lock.") {
		return true
	}
	for _, part := range strings.Split(strings.ToLower(p), "/") {
		if part == "$recycle.bin" || part == ".trashes" || part == ".spotlight-v100" || part == ".fseventsd" {
			return true
		}
	}
	return false
}

// ---- the FileSystem -------------------------------------------------------------

type FS struct {
	S     *store.Store
	props *deadProps
}

func New(s *store.Store) *FS {
	return &FS{S: s, props: &deadProps{m: map[string]map[xml.Name]webdav.Property{}}}
}

func clean(name string) string { return strings.Trim(path.Clean("/"+name), "/") }

func notExist(err error) bool {
	return errors.Is(err, store.ErrNotFound) || errors.Is(err, fs.ErrNotExist)
}

func (f *FS) translate(ctx context.Context, err error) error {
	switch {
	case err == nil:
		return nil
	case notExist(err):
		return os.ErrNotExist
	case errors.Is(err, store.ErrExist):
		return os.ErrExist
	}
	return Note(ctx, err)
}

func (f *FS) Mkdir(ctx context.Context, name string, _ os.FileMode) error {
	return f.translate(ctx, f.S.Mkdir(ctx, clean(name)))
}

func (f *FS) RemoveAll(ctx context.Context, name string) error {
	p := clean(name)
	if IsJunk(p) {
		return nil
	}
	f.props.drop(p)
	err := f.S.Delete(ctx, p)
	if notExist(err) {
		return nil // x/net/webdav answers 404 itself after Stat
	}
	return f.translate(ctx, err)
}

func (f *FS) Rename(ctx context.Context, oldName, newName string) error {
	o, n := clean(oldName), clean(newName)
	err := f.S.Rename(ctx, o, n)
	if err == nil {
		f.props.move(o, n)
	}
	return f.translate(ctx, err)
}

func (f *FS) Stat(ctx context.Context, name string) (os.FileInfo, error) {
	p := clean(name)
	if IsJunk(p) {
		return nil, os.ErrNotExist
	}
	in, err := f.S.Stat(ctx, p)
	if err != nil {
		return nil, f.translate(ctx, err)
	}
	if in == nil {
		return nil, os.ErrNotExist
	}
	return infoOf(in), nil
}

func (f *FS) OpenFile(ctx context.Context, name string, flag int, _ os.FileMode) (webdav.File, error) {
	p := clean(name)
	if flag&(os.O_WRONLY|os.O_RDWR|os.O_CREATE|os.O_TRUNC) != 0 {
		if p == "" {
			return nil, os.ErrPermission
		}
		if IsJunk(p) {
			log.Debugf("fichero del sistema descartado: %s", p)
			return &junkFile{name: path.Base(p)}, nil
		}
		parent, err := f.S.Stat(ctx, path.Dir("/"+p))
		if err != nil {
			return nil, f.translate(ctx, err)
		}
		if parent == nil || !parent.Dir {
			return nil, os.ErrNotExist
		}
		t := f.S.Tr.Start(transfers.Up, f.S.Account, p, 0)
		t.SetPhase("recibiendo")
		return &writeFile{fs: f, ctx: ctx, path: p, sp: store.NewSpool(f.S.Spool), t: t}, nil
	}
	if IsJunk(p) {
		return nil, os.ErrNotExist
	}
	in, err := f.S.Stat(ctx, p)
	if err != nil {
		return nil, f.translate(ctx, err)
	}
	if in == nil {
		return nil, os.ErrNotExist
	}
	return &readFile{fs: f, ctx: ctx, path: p, in: in}, nil
}

// ---- FileInfo ------------------------------------------------------------------

type info struct {
	name string
	size int64
	mod  time.Time
	dir  bool
	etag string
}

func infoOf(in *store.Info) *info {
	if in.Dir {
		return &info{name: in.Name, dir: true, mod: time.Now()}
	}
	e := in.Entry
	mod := time.Now()
	if e.Modified > 0 {
		mod = time.UnixMilli(e.Modified)
	}
	return &info{name: in.Name, size: e.Size, mod: mod, etag: e.ETag}
}

func (i *info) Name() string       { return i.name }
func (i *info) Size() int64        { return i.size }
func (i *info) ModTime() time.Time { return i.mod }
func (i *info) IsDir() bool        { return i.dir }
func (i *info) Sys() any           { return nil }
func (i *info) Mode() os.FileMode {
	if i.dir {
		return os.ModeDir | 0o755
	}
	return 0o644
}

// ContentType (webdav.ContentTyper): from the name, never sniffed (sniffing
// would download the start of every file on PROPFIND).
func (i *info) ContentType(context.Context) (string, error) {
	if i.dir {
		return "", webdav.ErrNotImplemented
	}
	if t := mime.TypeByExtension(strings.ToLower(path.Ext(i.name))); t != "" {
		return t, nil
	}
	return "application/octet-stream", nil
}

// ETag (webdav.ETager).
func (i *info) ETag(context.Context) (string, error) {
	if i.dir {
		return "", webdav.ErrNotImplemented
	}
	if i.etag != "" {
		return `"` + strings.ReplaceAll(i.etag, `"`, "") + `"`, nil
	}
	return fmt.Sprintf(`"%x%x"`, i.mod.UnixNano(), i.size), nil
}

// ---- files ---------------------------------------------------------------------

// readFile is a file or a folder opened for reading. Contents are streamed
// from O2 on demand (only x/net/webdav's COPY of folders reads them; GET is
// served by the HTTP layer with exact ranges).
type readFile struct {
	fs   *FS
	ctx  context.Context
	path string
	in   *store.Info
	pos  int64
	rc   io.ReadCloser
	dirs []os.FileInfo
	done bool
}

func (r *readFile) Close() error {
	if r.rc != nil {
		r.rc.Close()
		r.rc = nil
	}
	return nil
}

func (r *readFile) Read(b []byte) (int, error) {
	if r.in.Dir {
		return 0, os.ErrInvalid
	}
	if r.pos >= r.in.Entry.Size {
		return 0, io.EOF
	}
	if r.rc == nil {
		r.fs.S.WaitSettled(r.ctx, r.in.Entry.ID)
		rc, err := r.fs.S.C.Open(r.ctx, r.in.Entry.ID, r.pos, -1)
		if err != nil {
			return 0, Note(r.ctx, err)
		}
		r.rc = rc
	}
	n, err := r.rc.Read(b)
	r.pos += int64(n)
	return n, err
}

func (r *readFile) Seek(off int64, whence int) (int64, error) {
	var size int64
	if !r.in.Dir {
		size = r.in.Entry.Size
	}
	np := r.pos
	switch whence {
	case io.SeekStart:
		np = off
	case io.SeekCurrent:
		np += off
	case io.SeekEnd:
		np = size + off
	}
	if np < 0 {
		return 0, os.ErrInvalid
	}
	if np != r.pos && r.rc != nil {
		r.rc.Close()
		r.rc = nil
	}
	r.pos = np
	return np, nil
}

func (r *readFile) Readdir(count int) ([]os.FileInfo, error) {
	if !r.in.Dir {
		return nil, os.ErrInvalid
	}
	if !r.done {
		subs, err := r.fs.S.SubDirs(r.ctx, r.in.FolderID)
		if err != nil {
			return nil, Note(r.ctx, err)
		}
		files, err := r.fs.S.Files(r.ctx, r.in.FolderID)
		if err != nil {
			return nil, Note(r.ctx, err)
		}
		for _, n := range subs {
			r.dirs = append(r.dirs, &info{name: n, dir: true, mod: time.Now()})
		}
		for i := range files {
			e := files[i]
			if IsJunk(e.Name) {
				continue
			}
			r.dirs = append(r.dirs, infoOf(&store.Info{Name: e.Name, Entry: &e}))
		}
		r.done = true
	}
	if count <= 0 {
		out := r.dirs
		r.dirs = nil
		return out, nil
	}
	if len(r.dirs) == 0 {
		return nil, io.EOF
	}
	n := min(count, len(r.dirs))
	out := r.dirs[:n]
	r.dirs = r.dirs[n:]
	return out, nil
}

func (r *readFile) Stat() (os.FileInfo, error) { return infoOf(r.in), nil }
func (r *readFile) Write([]byte) (int, error)  { return 0, os.ErrPermission }

// DeadProps (webdav.DeadPropsHolder): the properties a client set with
// PROPPATCH, plus the RFC 4331 quota on folders.
func (r *readFile) DeadProps() (map[xml.Name]webdav.Property, error) {
	m := r.fs.props.get(r.path)
	if r.in.Dir {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		q, err := r.fs.S.Quota(ctx)
		cancel()
		if err == nil {
			if m == nil {
				m = map[xml.Name]webdav.Property{}
			}
			free, used := q.Free, q.Used
			m[xml.Name{Space: "DAV:", Local: "quota-available-bytes"}] = webdav.Property{
				XMLName: xml.Name{Space: "DAV:", Local: "quota-available-bytes"}, InnerXML: []byte(fmt.Sprint(free))}
			m[xml.Name{Space: "DAV:", Local: "quota-used-bytes"}] = webdav.Property{
				XMLName: xml.Name{Space: "DAV:", Local: "quota-used-bytes"}, InnerXML: []byte(fmt.Sprint(used))}
		}
	}
	return m, nil
}

func (r *readFile) Patch(p []webdav.Proppatch) ([]webdav.Propstat, error) {
	return r.fs.props.patch(r.path, p), nil
}

// writeFile receives a PUT body into a spool; Close uploads it to O2.
type writeFile struct {
	fs     *FS
	ctx    context.Context
	path   string
	sp     *store.Spool
	t      *transfers.T
	closed bool
	entry  *store.Entry
}

func (w *writeFile) Write(b []byte) (int, error) {
	n, err := w.sp.Write(b)
	w.t.AddDone(int64(n))
	return n, err
}

func (w *writeFile) Close() error {
	if w.closed {
		return nil
	}
	w.closed = true
	defer w.sp.Close()
	w.t.Size = w.sp.Size()
	w.t.SetDone(0)
	e, err := w.fs.S.Put(w.ctx, w.path, w.sp, mtimeOf(w.ctx), w.t)
	w.fs.S.Tr.Finish(w.t, err)
	if err != nil {
		log.Warnf("PUT %s (%d B): %v", w.path, w.sp.Size(), err)
		return Note(w.ctx, err)
	}
	w.entry = e
	return nil
}

func (w *writeFile) Stat() (os.FileInfo, error) {
	mod := mtimeOf(w.ctx)
	if mod.IsZero() {
		mod = time.Now()
	}
	i := &info{name: path.Base(w.path), size: w.sp.Size(), mod: mod}
	if w.entry != nil {
		i.etag = w.entry.ETag
	}
	return i, nil
}

func (w *writeFile) Read([]byte) (int, error)                         { return 0, os.ErrPermission }
func (w *writeFile) Seek(int64, int) (int64, error)                   { return 0, os.ErrInvalid }
func (w *writeFile) Readdir(int) ([]os.FileInfo, error)               { return nil, os.ErrInvalid }
func (w *writeFile) DeadProps() (map[xml.Name]webdav.Property, error) { return nil, nil }
func (w *writeFile) Patch(p []webdav.Proppatch) ([]webdav.Propstat, error) {
	return w.fs.props.patch(w.path, p), nil
}

// junkFile swallows an OS junk file.
type junkFile struct {
	name string
	n    int64
}

func (j *junkFile) Write(b []byte) (int, error)        { j.n += int64(len(b)); return len(b), nil }
func (j *junkFile) Close() error                       { return nil }
func (j *junkFile) Read([]byte) (int, error)           { return 0, io.EOF }
func (j *junkFile) Seek(int64, int) (int64, error)     { return 0, nil }
func (j *junkFile) Readdir(int) ([]os.FileInfo, error) { return nil, os.ErrInvalid }
func (j *junkFile) Stat() (os.FileInfo, error) {
	return &info{name: j.name, size: j.n, mod: time.Now()}, nil
}

// ---- dead properties (PROPPATCH), in memory ----------------------------------------

type deadProps struct {
	mu sync.Mutex
	m  map[string]map[xml.Name]webdav.Property
}

func (d *deadProps) get(p string) map[xml.Name]webdav.Property {
	d.mu.Lock()
	defer d.mu.Unlock()
	src := d.m[p]
	if src == nil {
		return nil
	}
	out := make(map[xml.Name]webdav.Property, len(src))
	for k, v := range src {
		out[k] = v
	}
	return out
}

func (d *deadProps) patch(p string, patches []webdav.Proppatch) []webdav.Propstat {
	d.mu.Lock()
	defer d.mu.Unlock()
	ps := webdav.Propstat{Status: 200}
	for _, pp := range patches {
		for _, prop := range pp.Props {
			ps.Props = append(ps.Props, webdav.Property{XMLName: prop.XMLName})
			if pp.Remove {
				delete(d.m[p], prop.XMLName)
				continue
			}
			if d.m[p] == nil {
				d.m[p] = map[xml.Name]webdav.Property{}
			}
			d.m[p][prop.XMLName] = prop
		}
	}
	return []webdav.Propstat{ps}
}

func (d *deadProps) drop(p string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	for k := range d.m {
		if k == p || strings.HasPrefix(k, p+"/") {
			delete(d.m, k)
		}
	}
}

func (d *deadProps) move(from, to string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	for k, v := range d.m {
		if k == from || strings.HasPrefix(k, from+"/") {
			delete(d.m, k)
			d.m[to+strings.TrimPrefix(k, from)] = v
		}
	}
}

// Ensure the interfaces x/net/webdav looks for are implemented.
var (
	_ webdav.FileSystem      = (*FS)(nil)
	_ webdav.DeadPropsHolder = (*readFile)(nil)
	_ webdav.ContentTyper    = (*info)(nil)
	_ webdav.ETager          = (*info)(nil)
	_                        = sapi.Validating
)
