package store

import (
	"crypto/sha1"
	"encoding/hex"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// Write cache: after an upload O2 takes a few seconds (up to ~30) to process
// the file before it can be downloaded. For cache_seconds after a PUT, reads
// of that file are served from a local copy, so a client that reads right
// after writing gets it at once instead of waiting for O2. Content is kept in
// memory up to 256 MB; the rest goes to files in the cache dir and is
// streamed from there. After it expires, reads wait for O2's validation.

const cacheRAM = 256 << 20

type CacheEntry struct {
	Path    string
	Size    int64
	ETag    string
	Mod     int64 // ms
	MediaID string
	Data    []byte // in memory, or
	File    string // on disk
	expires time.Time
}

type Cache struct {
	mu   sync.Mutex
	ttl  time.Duration
	dir  string
	ram  int64
	m    map[string]*CacheEntry
	stop chan struct{}
}

// NewCache: ttl <= 0 disables it. Files left in dir by a previous run are
// orphans (the cache is in memory) and are removed.
func NewCache(ttlSeconds int, dir string) *Cache {
	c := &Cache{ttl: time.Duration(ttlSeconds) * time.Second, dir: dir, m: map[string]*CacheEntry{},
		stop: make(chan struct{})}
	if dir != "" {
		_ = os.MkdirAll(dir, 0o700)
		if ents, err := os.ReadDir(dir); err == nil {
			for _, e := range ents {
				if strings.HasSuffix(e.Name(), ".bin") {
					_ = os.Remove(filepath.Join(dir, e.Name()))
				}
			}
		}
	}
	go c.sweeper()
	return c
}

func (c *Cache) Enabled() bool      { return c != nil && c.ttl > 0 }
func (c *Cache) TTL() time.Duration { return c.ttl }

func (c *Cache) Close() {
	if c == nil {
		return
	}
	select {
	case <-c.stop:
	default:
		close(c.stop)
	}
	c.mu.Lock()
	for k := range c.m {
		c.dropLocked(k)
	}
	c.mu.Unlock()
}

func (c *Cache) sweeper() {
	t := time.NewTicker(5 * time.Second)
	defer t.Stop()
	for {
		select {
		case <-c.stop:
			return
		case <-t.C:
			now := time.Now()
			c.mu.Lock()
			for k, e := range c.m {
				if now.After(e.expires) {
					c.dropLocked(k)
				}
			}
			c.mu.Unlock()
		}
	}
}

func (c *Cache) dropLocked(k string) {
	e, ok := c.m[k]
	if !ok {
		return
	}
	delete(c.m, k)
	if e.Data != nil {
		c.ram -= int64(len(e.Data))
	}
	if e.File != "" {
		_ = os.Remove(e.File)
	}
}

func (c *Cache) diskPath(p string) string {
	h := sha1.Sum([]byte(p + "\x00" + time.Now().String()))
	return filepath.Join(c.dir, hex.EncodeToString(h[:])+".bin")
}

// Put keeps the body of a finished upload (taking over the spool's temp file
// when it has one).
func (c *Cache) Put(p string, sp *Spool, etag string, mod int64, id string) {
	if !c.Enabled() {
		return
	}
	e := &CacheEntry{Path: p, Size: sp.Size(), ETag: etag, Mod: mod, MediaID: id,
		expires: time.Now().Add(c.ttl)}
	c.mu.Lock()
	c.dropLocked(p)
	fitsRAM := sp.f == nil && c.ram+sp.size <= cacheRAM
	c.mu.Unlock()
	switch {
	case fitsRAM:
		e.Data = append([]byte(nil), sp.buf...)
	case c.dir == "":
		return // no disk dir: metadata would be useless without content
	case sp.f != nil:
		dst := c.diskPath(p)
		if !sp.adopt(dst) {
			return
		}
		e.File = dst
	default: // in RAM, but the memory budget is used up: spill to disk
		dst := c.diskPath(p)
		if err := os.WriteFile(dst, sp.buf, 0o600); err != nil {
			return
		}
		e.File = dst
	}
	c.mu.Lock()
	c.dropLocked(p)
	c.m[p] = e
	if e.Data != nil {
		c.ram += int64(len(e.Data))
	}
	c.mu.Unlock()
}

// Get returns a fresh entry for path (nil if none).
func (c *Cache) Get(p string) *CacheEntry {
	if !c.Enabled() {
		return nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.m[p]
	if e == nil || time.Now().After(e.expires) {
		return nil
	}
	cp := *e
	return &cp
}

// Drop forgets path and everything under it (delete, overwrite, rename).
func (c *Cache) Drop(p string) {
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	for k := range c.m {
		if k == p || strings.HasPrefix(k, p+"/") {
			c.dropLocked(k)
		}
	}
}

type CacheView struct {
	Account   string `json:"account"`
	Path      string `json:"path"`
	Size      int64  `json:"size"`
	Where     string `json:"where"`
	ExpiresIn int    `json:"expires"`
}

func (c *Cache) Snapshot(account string) []CacheView {
	if c == nil {
		return nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	var out []CacheView
	now := time.Now()
	for _, e := range c.m {
		where := "ram"
		if e.File != "" {
			where = "disco"
		}
		out = append(out, CacheView{Account: account, Path: e.Path, Size: e.Size, Where: where,
			ExpiresIn: int(e.expires.Sub(now).Seconds())})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	return out
}
