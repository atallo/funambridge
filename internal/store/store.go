// Package store maps paths ("a/b/c.bin", relative to the O2 root) onto O2
// folders and files for one account.
//
//   - Folders: O2 returns the whole folder tree in one call; it is cached and
//     updated with our own changes (re-read every few minutes).
//   - Files: a per-folder index (name -> entry), loaded once per folder and
//     kept in sync with our own uploads/deletes. Same-name copies left by
//     older versions are remembered (Dups) and removed on overwrite/delete.
//   - After an upload O2 validates the file for a few seconds; Settle tracks
//     that (get-validation-status) and reads of a file still validating wait
//     for it, instead of serving a local copy for a guessed time.
package store

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"hash/fnv"
	"io"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/atallo/funambridge/internal/logx"
	"github.com/atallo/funambridge/internal/sapi"
	"github.com/atallo/funambridge/internal/transfers"
)

var log = logx.New("store")

type Entry struct {
	ID       string
	Name     string
	Size     int64
	Modified int64 // ms
	Created  int64 // ms
	ETag     string
	Dups     []string // other items with the same name (to be removed)
}

// Info describes a path: a folder (Dir) or a file (*Entry).
type Info struct {
	Dir      bool
	FolderID string
	Name     string
	Entry    *Entry
}

type Store struct {
	C       *sapi.Client
	Account string
	Tr      *transfers.Tracker
	Spool   string // dir for PUT bodies

	treeMu sync.Mutex
	tree   *tree

	idxMu sync.Mutex
	idx   map[string]map[string]*Entry // folder id -> name -> entry
	idxAt map[string]time.Time

	putLocks [64]sync.Mutex

	setMu    sync.Mutex
	settling map[string]chan struct{}

	qMu   sync.Mutex
	quota sapi.Storage
	qAt   time.Time
}

func New(c *sapi.Client, account string, tr *transfers.Tracker, spool string) *Store {
	return &Store{C: c, Account: account, Tr: tr, Spool: spool,
		idx: map[string]map[string]*Entry{}, idxAt: map[string]time.Time{}, settling: map[string]chan struct{}{}}
}

// ---- folder tree -----------------------------------------------------------

type tree struct {
	root string
	kids map[string]map[string]string // parent id -> name -> id
	name map[string]string            // id -> name
	par  map[string]string            // id -> parent id
	at   time.Time
}

const treeTTL = 3 * time.Minute

func (s *Store) folders(ctx context.Context) (*tree, error) {
	s.treeMu.Lock()
	defer s.treeMu.Unlock()
	if s.tree != nil && time.Since(s.tree.at) < treeTTL {
		return s.tree, nil
	}
	root, err := s.C.RootID(ctx)
	if err != nil {
		return nil, err
	}
	all, err := s.C.AllFolders(ctx)
	if err != nil {
		if s.tree != nil { // keep serving the last tree while O2 hiccups
			log.Debugf("árbol de carpetas: %v (se usa el anterior)", err)
			return s.tree, nil
		}
		return nil, err
	}
	t := &tree{root: root, kids: map[string]map[string]string{}, name: map[string]string{},
		par: map[string]string{}, at: time.Now()}
	for _, f := range all {
		if f.ID == root {
			continue
		}
		t.add(f.ParentID, f.Name, f.ID)
	}
	s.tree = t
	return t, nil
}

func (t *tree) add(parent, name, id string) {
	if t.kids[parent] == nil {
		t.kids[parent] = map[string]string{}
	}
	t.kids[parent][name] = id
	t.name[id], t.par[id] = name, parent
}

func (t *tree) remove(id string) {
	if p, ok := t.par[id]; ok {
		delete(t.kids[p], t.name[id])
	}
	for _, kid := range t.kids[id] {
		t.remove(kid)
	}
	delete(t.kids, id)
	delete(t.name, id)
	delete(t.par, id)
}

func (s *Store) treeAge() time.Duration {
	s.treeMu.Lock()
	defer s.treeMu.Unlock()
	if s.tree == nil {
		return 0
	}
	return time.Since(s.tree.at)
}

// InvalidateTree forces a re-read of the folders on next use.
func (s *Store) InvalidateTree() {
	s.treeMu.Lock()
	if s.tree != nil {
		s.tree.at = time.Time{}
	}
	s.treeMu.Unlock()
}

func split(p string) []string {
	var out []string
	for _, x := range strings.Split(strings.Trim(p, "/"), "/") {
		if x != "" && x != "." {
			out = append(out, x)
		}
	}
	return out
}

// Dir returns the folder id of path ("" + nil if it does not exist). With
// create, missing folders are created.
func (s *Store) Dir(ctx context.Context, p string, create bool) (string, error) {
	t, err := s.folders(ctx)
	if err != nil {
		return "", err
	}
	cur := t.root
	reloaded := false
	for _, part := range split(p) {
		s.treeMu.Lock()
		id, ok := t.kids[cur][part]
		s.treeMu.Unlock()
		if !ok && !reloaded && (create || s.treeAge() > 30*time.Second) {
			// it may exist already (created outside the proxy since the tree was
			// read): re-read before creating a second folder with that name or
			// answering "not found"
			reloaded = true
			s.InvalidateTree()
			if t, err = s.folders(ctx); err != nil {
				return "", err
			}
			s.treeMu.Lock()
			id, ok = t.kids[cur][part]
			s.treeMu.Unlock()
		}
		if !ok {
			if !create {
				return "", nil
			}
			id, err = s.C.CreateFolder(ctx, cur, part)
			if err != nil {
				return "", err
			}
			s.treeMu.Lock()
			t.add(cur, part, id)
			s.treeMu.Unlock()
		}
		cur = id
	}
	return cur, nil
}

// SubDirs lists the sub-folder names of a folder.
func (s *Store) SubDirs(ctx context.Context, fid string) ([]string, error) {
	t, err := s.folders(ctx)
	if err != nil {
		return nil, err
	}
	s.treeMu.Lock()
	defer s.treeMu.Unlock()
	out := make([]string, 0, len(t.kids[fid]))
	for n := range t.kids[fid] {
		out = append(out, n)
	}
	sort.Strings(out)
	return out, nil
}

// ---- per-folder file index --------------------------------------------------

func (s *Store) index(ctx context.Context, fid string) (map[string]*Entry, error) {
	s.idxMu.Lock()
	if ix, ok := s.idx[fid]; ok {
		s.idxMu.Unlock()
		return ix, nil
	}
	s.idxMu.Unlock()
	ms, err := s.C.ListMedia(ctx, fid)
	if err != nil {
		return nil, err
	}
	ix := map[string]*Entry{}
	for _, m := range ms {
		e := &Entry{ID: m.ID, Name: m.Name, Size: m.Size, Modified: m.Modified, Created: m.Created, ETag: m.ETag}
		if prev := ix[m.Name]; prev != nil {
			// same name twice (left by an old failed upload): serve the newest,
			// remember the rest so overwrite/delete remove them
			if e.Modified >= prev.Modified {
				e.Dups = append(append(prev.Dups, prev.ID), e.Dups...)
			} else {
				prev.Dups = append(prev.Dups, e.ID)
				e = prev
			}
		}
		ix[m.Name] = e
	}
	s.idxMu.Lock()
	defer s.idxMu.Unlock()
	if cur, ok := s.idx[fid]; ok {
		return cur, nil
	}
	s.idx[fid] = ix
	s.idxAt[fid] = time.Now()
	return ix, nil
}

// lookupForWrite is lookup that, before treating a name as new, re-reads a
// folder index older than 10 minutes: a file created outside the proxy since
// then must be overwritten, not duplicated.
func (s *Store) lookupForWrite(ctx context.Context, fid, name string) (*Entry, error) {
	e, err := s.lookup(ctx, fid, name)
	if err != nil || e != nil {
		return e, err
	}
	s.idxMu.Lock()
	stale := time.Since(s.idxAt[fid]) > 10*time.Minute
	if stale {
		delete(s.idx, fid)
	}
	s.idxMu.Unlock()
	if !stale {
		return nil, nil
	}
	return s.lookup(ctx, fid, name)
}

func (s *Store) lookup(ctx context.Context, fid, name string) (*Entry, error) {
	ix, err := s.index(ctx, fid)
	if err != nil {
		return nil, err
	}
	s.idxMu.Lock()
	defer s.idxMu.Unlock()
	if e := ix[name]; e != nil {
		c := *e
		c.Dups = append([]string(nil), e.Dups...)
		return &c, nil
	}
	return nil, nil
}

func (s *Store) setEntry(fid string, e *Entry) {
	s.idxMu.Lock()
	defer s.idxMu.Unlock()
	if ix, ok := s.idx[fid]; ok {
		ix[e.Name] = e
	}
}

func (s *Store) dropEntry(fid, name string) {
	s.idxMu.Lock()
	defer s.idxMu.Unlock()
	if ix, ok := s.idx[fid]; ok {
		delete(ix, name)
	}
}

// Files lists a folder's files (sorted by name).
func (s *Store) Files(ctx context.Context, fid string) ([]Entry, error) {
	ix, err := s.index(ctx, fid)
	if err != nil {
		return nil, err
	}
	s.idxMu.Lock()
	out := make([]Entry, 0, len(ix))
	for _, e := range ix {
		out = append(out, *e)
	}
	s.idxMu.Unlock()
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

// Stat describes a path; (nil, nil) if it does not exist.
func (s *Store) Stat(ctx context.Context, p string) (*Info, error) {
	parts := split(p)
	if len(parts) == 0 {
		t, err := s.folders(ctx)
		if err != nil {
			return nil, err
		}
		return &Info{Dir: true, FolderID: t.root, Name: ""}, nil
	}
	if fid, err := s.Dir(ctx, p, false); err != nil {
		return nil, err
	} else if fid != "" {
		return &Info{Dir: true, FolderID: fid, Name: parts[len(parts)-1]}, nil
	}
	parent, err := s.Dir(ctx, strings.Join(parts[:len(parts)-1], "/"), false)
	if err != nil || parent == "" {
		return nil, err
	}
	name := parts[len(parts)-1]
	e, err := s.lookup(ctx, parent, name)
	if err != nil || e == nil {
		return nil, err
	}
	return &Info{FolderID: parent, Name: name, Entry: e}, nil
}

// ---- quota -------------------------------------------------------------------

func (s *Store) Quota(ctx context.Context) (sapi.Storage, error) {
	s.qMu.Lock()
	defer s.qMu.Unlock()
	if time.Since(s.qAt) < time.Minute {
		return s.quota, nil
	}
	q, err := s.C.Storage(ctx)
	if err != nil {
		if !s.qAt.IsZero() {
			return s.quota, nil
		}
		return q, err
	}
	s.quota, s.qAt = q, time.Now()
	return q, nil
}

// ---- validation tracking -------------------------------------------------------

// Settle follows a freshly written file until O2 finishes validating it
// (polling get-validation-status, like the official app); reads wait for it.
func (s *Store) Settle(id, fid, name string) {
	ch := make(chan struct{})
	s.setMu.Lock()
	if old, ok := s.settling[id]; ok {
		close(old)
	}
	s.settling[id] = ch
	s.setMu.Unlock()
	go func() {
		defer func() {
			s.setMu.Lock()
			if s.settling[id] == ch {
				delete(s.settling, id)
				close(ch)
			}
			s.setMu.Unlock()
		}()
		wait, fails := 3*time.Second, 0
		start := time.Now()
		for time.Since(start) < 30*time.Minute {
			time.Sleep(wait)
			s.setMu.Lock()
			current := s.settling[id] == ch
			s.setMu.Unlock()
			if !current {
				return // a newer upload of the same item is followed instead
			}
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			st, err := s.C.ValidationStatus(ctx, id, fid)
			cancel()
			if err != nil {
				if fails++; fails >= 20 {
					log.Warnf("se deja de seguir la validación de '%s': %v", name, err)
					return
				}
			} else if !sapi.Validating(st) {
				log.Debugf("validación de '%s' terminada (%s) en %s", name, st,
					time.Since(start).Round(time.Second))
				return
			}
			if wait < 30*time.Second {
				wait = wait * 3 / 2
			}
		}
		log.Warnf("'%s' sigue en validación en O2 tras 30 min", name)
	}()
}

// WaitSettled blocks while id is still being validated (bounded by ctx).
func (s *Store) WaitSettled(ctx context.Context, id string) {
	s.setMu.Lock()
	ch, ok := s.settling[id]
	s.setMu.Unlock()
	if !ok {
		return
	}
	select {
	case <-ch:
	case <-ctx.Done():
	}
}

func (s *Store) Settling(id string) bool {
	s.setMu.Lock()
	defer s.setMu.Unlock()
	_, ok := s.settling[id]
	return ok
}

// ---- writes --------------------------------------------------------------------

func (s *Store) putLock(fid, name string) *sync.Mutex {
	h := fnv.New32a()
	h.Write([]byte(fid + "\x00" + name))
	return &s.putLocks[h.Sum32()%uint32(len(s.putLocks))]
}

var ErrNotFound = errors.New("no existe")
var ErrExist = errors.New("ya existe")

// Put uploads sp as path p (creating missing folders). An existing file is
// overwritten in place (same O2 id); extra same-name copies are removed.
func (s *Store) Put(ctx context.Context, p string, sp *Spool, mod time.Time, t *transfers.T) (*Entry, error) {
	parts := split(p)
	if len(parts) == 0 {
		return nil, errors.New("no se puede escribir en la raíz")
	}
	name := parts[len(parts)-1]
	fid, err := s.Dir(ctx, strings.Join(parts[:len(parts)-1], "/"), true)
	if err != nil {
		return nil, err
	}
	mu := s.putLock(fid, name)
	mu.Lock()
	defer mu.Unlock()
	old, err := s.lookupForWrite(ctx, fid, name)
	if err != nil {
		return nil, err
	}
	if old != nil && sp.Size() == 0 {
		// O2 cannot overwrite with 0 bytes (the original would be lost):
		// remove it and create a new empty one
		s.deleteIDs(ctx, append([]string{old.ID}, old.Dups...))
		s.dropEntry(fid, name)
		old = nil
	}
	release, err := s.Tr.Acquire(ctx, t)
	if err != nil {
		return nil, err
	}
	defer release()
	req := sapi.UploadReq{FolderID: fid, Name: name, Size: sp.Size(), Open: sp.OpenAt, ModTime: mod,
		Progress: t.SetDone, Phase: t.SetPhase}
	if old != nil {
		req.Existing = &sapi.Media{ID: old.ID, Created: old.Created}
		s.WaitSettled(ctx, old.ID) // O2 refuses to touch a file it is still validating
	}
	res, err := s.C.Upload(ctx, req)
	if err != nil && old != nil && !sapi.IsTransient(err) && !sapi.IsSession(err) && ctx.Err() == nil {
		// the old item may be gone (deleted elsewhere): retry as a new file
		if m, gerr := s.C.GetMedia(ctx, old.ID, "name"); gerr == nil && m == nil {
			log.Infof("'%s' ya no existía en O2; se sube como nuevo", p)
			req.Existing, old = nil, nil
			res, err = s.C.Upload(ctx, req)
		}
	}
	if err != nil {
		return nil, err
	}
	if old != nil && len(old.Dups) > 0 {
		s.deleteIDs(ctx, old.Dups)
		log.Infof("borradas %d copias de más de '%s'", len(old.Dups), p)
	}
	now := time.Now().UnixMilli()
	created := now
	if old != nil && old.Created > 0 {
		created = old.Created
	}
	m := now
	if !mod.IsZero() {
		m = mod.UnixMilli()
	}
	e := &Entry{ID: res.ID, Name: name, Size: sp.Size(), Modified: m, Created: created, ETag: res.ETag}
	s.setEntry(fid, e)
	t.SetPhase("validando")
	s.Settle(res.ID, fid, p)
	s.qMu.Lock()
	s.qAt = time.Time{}
	s.qMu.Unlock()
	return e, nil
}

func (s *Store) deleteIDs(ctx context.Context, ids []string) {
	for _, id := range ids {
		if err := s.C.DeleteMedia(ctx, id); err != nil {
			log.Debugf("borrar %s: %v", id, err)
		}
	}
}

// Mkdir creates a folder; ErrExist if the path exists.
func (s *Store) Mkdir(ctx context.Context, p string) error {
	if in, err := s.Stat(ctx, p); err != nil {
		return err
	} else if in != nil {
		return ErrExist
	}
	parts := split(p)
	parent, err := s.Dir(ctx, strings.Join(parts[:len(parts)-1], "/"), false)
	if err != nil {
		return err
	}
	if parent == "" {
		return ErrNotFound
	}
	_, err = s.Dir(ctx, p, true)
	return err
}

// Delete removes a file (all same-name copies) or a folder (to the trash).
func (s *Store) Delete(ctx context.Context, p string) error {
	in, err := s.Stat(ctx, p)
	if err != nil {
		return err
	}
	if in == nil {
		return ErrNotFound
	}
	if in.Dir {
		if len(split(p)) == 0 {
			return errors.New("no se borra la raíz")
		}
		if err := s.deleteFolder(ctx, in.FolderID, p); err != nil {
			return err
		}
		s.treeMu.Lock()
		if s.tree != nil {
			s.tree.remove(in.FolderID)
		}
		s.treeMu.Unlock()
		return nil
	}
	e := in.Entry
	if err := s.C.DeleteMedia(ctx, e.ID); err != nil {
		return err
	}
	s.deleteIDs(ctx, e.Dups)
	s.dropEntry(in.FolderID, in.Name)
	return nil
}

// deleteFolder: O2 refuses to delete at once a folder with more items than
// folders.softdelete-updates-limit (FOL-1036): empty it first, then delete.
func (s *Store) deleteFolder(ctx context.Context, fid, label string) error {
	err := s.C.DeleteFolder(ctx, fid)
	if errors.Is(err, sapi.ErrFolderTooBig) {
		log.Infof("'%s' tiene demasiados elementos para borrarla de una vez (FOL-1036); se vacía por partes", label)
		subs, serr := s.SubDirs(ctx, fid)
		if serr != nil {
			return serr
		}
		for _, n := range subs {
			s.treeMu.Lock()
			id := s.tree.kids[fid][n]
			s.treeMu.Unlock()
			if err := s.deleteFolder(ctx, id, label+"/"+n); err != nil {
				return err
			}
		}
		ms, lerr := s.C.ListMedia(ctx, fid)
		if lerr != nil {
			return lerr
		}
		for _, m := range ms {
			if err := s.C.DeleteMedia(ctx, m.ID); err != nil {
				return err
			}
		}
		err = s.C.DeleteFolder(ctx, fid)
	}
	if err == nil {
		s.idxMu.Lock()
		delete(s.idx, fid)
		s.idxMu.Unlock()
	}
	return err
}

// Rename moves/renames a file or folder (the destination must not exist).
func (s *Store) Rename(ctx context.Context, from, to string) error {
	src, err := s.Stat(ctx, from)
	if err != nil {
		return err
	}
	if src == nil {
		return ErrNotFound
	}
	tp := split(to)
	if len(tp) == 0 {
		return errors.New("destino no válido")
	}
	newName := tp[len(tp)-1]
	dstParent, err := s.Dir(ctx, strings.Join(tp[:len(tp)-1], "/"), false)
	if err != nil {
		return err
	}
	if dstParent == "" {
		return ErrNotFound
	}
	if src.Dir {
		if err := s.C.UpdateFolder(ctx, src.FolderID, newName, dstParent); err != nil {
			return err
		}
		s.InvalidateTree()
		return nil
	}
	e := src.Entry
	s.WaitSettled(ctx, e.ID)
	move := ""
	if dstParent != src.FolderID {
		move = dstParent
	}
	if err := s.C.RenameMedia(ctx, e.ID, e.Name, newName, move); err != nil {
		return err
	}
	s.dropEntry(src.FolderID, e.Name)
	ne := *e
	ne.Name, ne.Dups = newName, nil
	s.setEntry(dstParent, &ne)
	s.deleteIDs(ctx, e.Dups)
	return nil
}

// Copy copies a file inside O2 (no download/upload). The destination must not
// exist.
func (s *Store) Copy(ctx context.Context, from, to string) error {
	src, err := s.Stat(ctx, from)
	if err != nil {
		return err
	}
	if src == nil || src.Dir {
		return ErrNotFound
	}
	tp := split(to)
	name := tp[len(tp)-1]
	dst, err := s.Dir(ctx, strings.Join(tp[:len(tp)-1], "/"), false)
	if err != nil {
		return err
	}
	if dst == "" {
		return ErrNotFound
	}
	s.WaitSettled(ctx, src.Entry.ID)
	id, err := s.C.CopyMedia(ctx, src.Entry.ID, dst, name)
	if err != nil {
		return err
	}
	if id == "" { // O2 did not say: re-read that folder
		s.idxMu.Lock()
		delete(s.idx, dst)
		s.idxMu.Unlock()
		return nil
	}
	now := time.Now().UnixMilli()
	s.setEntry(dst, &Entry{ID: id, Name: name, Size: src.Entry.Size, Modified: now, Created: now,
		ETag: src.Entry.ETag})
	s.Settle(id, dst, to)
	return nil
}

// ---- spool: a PUT body kept for (re)sending ------------------------------------

const spoolRAM = 16 << 20

// Spool holds an upload body: in memory up to 16 MB, else in a temp file, so
// the upload can be resumed from any offset.
type Spool struct {
	dir  string
	buf  []byte
	f    *os.File
	size int64
}

func NewSpool(dir string) *Spool { return &Spool{dir: dir} }

func (sp *Spool) Write(b []byte) (int, error) {
	if sp.f == nil && len(sp.buf)+len(b) <= spoolRAM {
		sp.buf = append(sp.buf, b...)
		sp.size += int64(len(b))
		return len(b), nil
	}
	if sp.f == nil {
		f, err := os.CreateTemp(sp.dir, "funambridge-put-*.tmp")
		if err != nil {
			return 0, fmt.Errorf("spool: %w", err)
		}
		if _, err := f.Write(sp.buf); err != nil {
			f.Close()
			os.Remove(f.Name())
			return 0, err
		}
		sp.f, sp.buf = f, nil
	}
	n, err := sp.f.Write(b)
	sp.size += int64(n)
	return n, err
}

func (sp *Spool) Size() int64 { return sp.size }

func (sp *Spool) OpenAt(off int64) (io.Reader, error) {
	if sp.f == nil {
		return bytes.NewReader(sp.buf[off:]), nil
	}
	return io.NewSectionReader(sp.f, off, sp.size-off), nil
}

func (sp *Spool) Close() {
	if sp.f != nil {
		name := sp.f.Name()
		sp.f.Close()
		os.Remove(name)
		sp.f = nil
	}
	sp.buf = nil
}
