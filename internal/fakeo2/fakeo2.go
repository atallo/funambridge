// Package fakeo2 is an in-memory imitation of O2 Cloud's SAPI for tests. It
// reproduces the behaviours funambridge depends on: flat folder list, upload
// in two steps with resumable content (308 + Range), in-place overwrite,
// validation that ends a moment after the upload, validation-key rotation
// (SEC-1003), FOL-1036 on big folders, and byte ranges on downloads.
package fakeo2

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

type Item struct {
	ID, Name, Folder string
	Size             int64 // declared
	Data             []byte
	Created          int64
	Modified         int64
	Validated        time.Time
	Deleted          bool
}

type Server struct {
	*httptest.Server
	Mu       sync.Mutex
	Key      string
	Folders  map[string][2]string // id -> {name, parent}
	Items    map[string]*Item
	next     int
	Root     string
	FolderOp int // FOL-1036 above this many items

	// fault injection
	CutAfter    int64 // cut the next content POST after this many bytes (0 = off)
	FailContent int   // answer 500 to the next N content POSTs
	RotateNext  bool  // answer SEC-1003 to the next call
	SessionDead bool  // answer 401 + HTML to everything
	ValidateFor time.Duration

	// observation
	Calls         map[string]int
	activeUploads int
	MaxConcurrent int
	UploadDelay   time.Duration
}

func New() *Server {
	s := &Server{Key: "k1", Folders: map[string][2]string{}, Items: map[string]*Item{},
		next: 100, Root: "1", FolderOp: 1000, Calls: map[string]int{}, ValidateFor: 300 * time.Millisecond}
	s.Folders["1"] = [2]string{"", "0"}
	s.Server = httptest.NewServer(http.HandlerFunc(s.handle))
	return s
}

func (s *Server) id() string { s.next++; return strconv.Itoa(s.next) }

func write(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func apiErr(w http.ResponseWriter, code int, c, msg string) {
	write(w, code, map[string]any{"error": map[string]any{"code": c, "message": msg}})
}

func ok(w http.ResponseWriter, data any) {
	write(w, 200, map[string]any{"data": data, "responsetime": time.Now().UnixMilli()})
}

// Mkdir adds a folder under parent and returns its id.
func (s *Server) Mkdir(parent, name string) string {
	s.Mu.Lock()
	defer s.Mu.Unlock()
	id := s.id()
	s.Folders[id] = [2]string{name, parent}
	return id
}

// Put adds a validated file directly.
func (s *Server) Put(folder, name string, data []byte, modified int64) string {
	s.Mu.Lock()
	defer s.Mu.Unlock()
	id := s.id()
	s.Items[id] = &Item{ID: id, Name: name, Folder: folder, Size: int64(len(data)), Data: data,
		Created: modified, Modified: modified}
	return id
}

// Files returns the live (not deleted) items of a folder, sorted by name.
func (s *Server) Files(folder string) []Item {
	s.Mu.Lock()
	defer s.Mu.Unlock()
	var out []Item
	for _, it := range s.Items {
		if it.Folder == folder && !it.Deleted {
			out = append(out, *it)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].Name < out[j].Name || (out[i].Name == out[j].Name && out[i].ID < out[j].ID)
	})
	return out
}

func (s *Server) handle(w http.ResponseWriter, r *http.Request) {
	action := r.URL.Query().Get("action")
	s.Mu.Lock()
	s.Calls[r.URL.Path+"?"+action]++
	dead, rotate := s.SessionDead, s.RotateNext
	s.RotateNext = false
	key := s.Key
	s.Mu.Unlock()
	if dead {
		w.Header().Set("Content-Type", "text/html")
		w.WriteHeader(401)
		_, _ = io.WriteString(w, "<html>login</html>")
		return
	}
	if strings.HasPrefix(r.URL.Path, "/dl/") {
		s.download(w, r)
		return
	}
	if r.URL.Path == "/sapi/system/information" {
		write(w, 200, map[string]any{"sapi.upload.max-size-in-mb": "4096", "folders.softdelete-updates-limit": strconv.Itoa(s.FolderOp),
			"trash.cleanup.items-expire-time-in-minutes": "86400", "sapi.upload.endpoint": s.URL})
		return
	}
	if rotate {
		s.Mu.Lock()
		s.Key = "k" + s.id()
		nk := s.Key
		s.Mu.Unlock()
		write(w, 401, map[string]any{"error": map[string]any{"code": "SEC-1003",
			"message": "Invalid mandatory validation key", "data": nk}})
		return
	}
	if r.URL.Query().Get("validationkey") != key {
		write(w, 401, map[string]any{"error": map[string]any{"code": "SEC-1003",
			"message": "Invalid mandatory validation key", "data": key}})
		return
	}
	switch {
	case r.URL.Path == "/sapi/profile":
		ok(w, map[string]any{"user": map[string]any{}})
	case r.URL.Path == "/sapi/media/folder/root":
		ok(w, map[string]any{"folders": []any{map[string]any{"id": json.Number(s.Root), "name": ""}}})
	case r.URL.Path == "/sapi/media/folder" && action == "get":
		s.Mu.Lock()
		var fs []any
		for id, f := range s.Folders {
			fs = append(fs, map[string]any{"id": json.Number(id), "name": f[0], "parentid": json.Number(f[1])})
		}
		s.Mu.Unlock()
		ok(w, map[string]any{"folders": fs})
	case r.URL.Path == "/sapi/media/folder" && action == "save":
		var b struct{ Data map[string]any }
		_ = json.NewDecoder(r.Body).Decode(&b)
		s.Mu.Lock()
		id := fmt.Sprint(b.Data["id"])
		if b.Data["id"] == nil {
			id = s.id()
		}
		s.Folders[id] = [2]string{fmt.Sprint(b.Data["name"]), fmt.Sprint(b.Data["parentid"])}
		s.Mu.Unlock()
		ok(w, map[string]any{"id": json.Number(id)})
	case r.URL.Path == "/sapi/media/folder" && action == "softdelete":
		id := r.URL.Query().Get("id")
		s.Mu.Lock()
		n := 0
		for _, it := range s.Items {
			if it.Folder == id && !it.Deleted {
				n++
			}
		}
		for _, f := range s.Folders {
			if f[1] == id {
				n++
			}
		}
		if n > s.FolderOp {
			s.Mu.Unlock()
			write(w, 200, map[string]any{"error": map[string]any{"code": "FOL-1036", "message": "too many"}})
			return
		}
		delete(s.Folders, id)
		s.Mu.Unlock()
		ok(w, map[string]any{})
	case r.URL.Path == "/sapi/media" && action == "get":
		s.mediaGet(w, r)
	case r.URL.Path == "/sapi/media" && action == "softdelete":
		s.Mu.Lock()
		if it := s.Items[r.URL.Query().Get("id")]; it != nil {
			it.Deleted = true
		}
		s.Mu.Unlock()
		ok(w, map[string]any{})
	case r.URL.Path == "/sapi/media" && action == "get-storage-space":
		ok(w, map[string]any{"quota": 1000000, "used": 1234, "free": 998766, "softdeleted": 10})
	case r.URL.Path == "/sapi/media" && action == "get-validation-status":
		var b struct {
			Data struct{ IDs []map[string]any } `json:"data"`
		}
		_ = json.NewDecoder(r.Body).Decode(&b)
		var out []any
		s.Mu.Lock()
		for _, e := range b.Data.IDs {
			id := fmt.Sprint(e["id"])
			if it := s.Items[id]; it != nil && !it.Deleted {
				st := "U"
				if time.Now().Before(it.Validated) {
					st = "V"
				}
				out = append(out, map[string]any{"id": json.Number(id), "status": st})
			}
		}
		s.Mu.Unlock()
		ok(w, map[string]any{"ids": out})
	case r.URL.Path == "/sapi/media/file" && action == "copy":
		var b struct{ Data map[string]any }
		_ = json.NewDecoder(r.Body).Decode(&b)
		s.Mu.Lock()
		src := s.Items[fmt.Sprint(b.Data["id"])]
		id := s.id()
		cp := *src
		cp.ID, cp.Folder, cp.Name = id, fmt.Sprint(b.Data["parentid"]), fmt.Sprint(b.Data["name"])
		s.Items[id] = &cp
		s.Mu.Unlock()
		ok(w, map[string]any{"id": json.Number(id)})
	case strings.HasPrefix(r.URL.Path, "/sapi/upload/") && action == "save-metadata":
		var b struct{ Data map[string]any }
		_ = json.NewDecoder(r.Body).Decode(&b)
		s.Mu.Lock()
		var it *Item
		if b.Data["id"] != nil {
			it = s.Items[fmt.Sprint(b.Data["id"])]
			if it == nil {
				s.Mu.Unlock()
				apiErr(w, 200, "MED-1001", "no such media")
				return
			}
			if n, ok := b.Data["name"]; ok {
				it.Name = fmt.Sprint(n)
			}
			if f, ok := b.Data["folderid"]; ok {
				it.Folder = fmt.Sprint(f)
			}
			if sz, ok := b.Data["size"]; ok {
				it.Size, it.Data = int64(sz.(float64)), nil
			}
		} else {
			it = &Item{ID: s.id(), Name: fmt.Sprint(b.Data["name"]), Folder: fmt.Sprint(b.Data["folderid"]),
				Size: int64(b.Data["size"].(float64)), Created: time.Now().UnixMilli()}
			s.Items[it.ID] = it
		}
		it.Modified = time.Now().UnixMilli()
		id := it.ID
		s.Mu.Unlock()
		write(w, 200, map[string]any{"success": "Media metadata saved successfully", "id": id})
	case strings.HasPrefix(r.URL.Path, "/sapi/upload/") && action == "save" && r.Method == "GET":
		s.offset(w, r)
	case strings.HasPrefix(r.URL.Path, "/sapi/upload/") && action == "save":
		s.content(w, r)
	default:
		apiErr(w, 404, "COM-404", "unknown "+r.URL.Path+" "+action)
	}
}

func (s *Server) mediaGet(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Data struct {
			IDs    []any    `json:"ids"`
			Fields []string `json:"fields"`
		} `json:"data"`
	}
	_ = json.NewDecoder(r.Body).Decode(&b)
	s.Mu.Lock()
	defer s.Mu.Unlock()
	view := func(it *Item) map[string]any {
		return map[string]any{"id": json.Number(it.ID), "name": it.Name, "size": it.Size,
			"creationdate": it.Created, "modificationdate": it.Modified, "etag": "e" + it.ID,
			"url": "/dl/" + it.ID + "?k=secret"}
	}
	var out []any
	if len(b.Data.IDs) > 0 {
		for _, id := range b.Data.IDs {
			if it := s.Items[fmt.Sprint(id)]; it != nil && !it.Deleted {
				out = append(out, view(it))
			}
		}
		ok(w, map[string]any{"media": out})
		return
	}
	folder := r.URL.Query().Get("folderid")
	var ids []string
	for id, it := range s.Items {
		if it.Folder == folder && !it.Deleted {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	off, _ := strconv.Atoi(r.URL.Query().Get("offset"))
	lim, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	if lim == 0 {
		lim = 200
	}
	end := min(off+lim, len(ids))
	for _, id := range ids[min(off, len(ids)):end] {
		out = append(out, view(s.Items[id]))
	}
	ok(w, map[string]any{"media": out, "more": end < len(ids)})
}

func (s *Server) offset(w http.ResponseWriter, r *http.Request) {
	s.Mu.Lock()
	it := s.Items[r.Header.Get("x-funambol-id")]
	s.Mu.Unlock()
	if it == nil {
		apiErr(w, 200, "MED-1001", "no such media")
		return
	}
	if int64(len(it.Data)) >= it.Size && it.Size > 0 {
		write(w, 200, map[string]any{"id": it.ID, "status": "V", "etag": "e" + it.ID})
		return
	}
	if len(it.Data) > 0 {
		w.Header().Set("Range", fmt.Sprintf("0-%d", len(it.Data)-1))
	}
	w.WriteHeader(308)
}

func (s *Server) content(w http.ResponseWriter, r *http.Request) {
	s.Mu.Lock()
	it := s.Items[r.Header.Get("x-funambol-id")]
	s.activeUploads++
	s.MaxConcurrent = max(s.MaxConcurrent, s.activeUploads)
	fail := s.FailContent > 0
	if fail {
		s.FailContent--
	}
	cut := s.CutAfter
	s.CutAfter = 0
	delay := s.UploadDelay
	s.Mu.Unlock()
	defer func() { s.Mu.Lock(); s.activeUploads--; s.Mu.Unlock() }()
	if delay > 0 {
		time.Sleep(delay)
	}
	if it == nil {
		apiErr(w, 200, "MED-1001", "no such media")
		return
	}
	if fail {
		_, _ = io.Copy(io.Discard, r.Body)
		apiErr(w, 500, "PAPI-0000", "internal")
		return
	}
	var start int64
	if cr := r.Header.Get("content-range"); cr != "" {
		fmt.Sscanf(cr, "bytes %d-", &start)
	}
	var body []byte
	if cut > 0 {
		body = make([]byte, cut)
		n, _ := io.ReadFull(r.Body, body)
		body = body[:n]
	} else {
		body, _ = io.ReadAll(r.Body)
	}
	s.Mu.Lock()
	if start == 0 {
		it.Data = nil
	}
	if int64(len(it.Data)) == start {
		it.Data = append(it.Data, body...)
	}
	s.Mu.Unlock()
	if cut > 0 { // simulate the connection dying mid-body
		if hj, ok := w.(http.Hijacker); ok {
			c, _, _ := hj.Hijack()
			c.Close()
			return
		}
	}
	s.Mu.Lock()
	it.Validated = time.Now().Add(s.ValidateFor)
	s.Mu.Unlock()
	write(w, 200, map[string]any{"success": "Media uploaded successfully", "id": it.ID, "status": "V", "etag": "e" + it.ID})
}

func (s *Server) download(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/dl/")
	s.Mu.Lock()
	it := s.Items[id]
	s.Calls["download"]++
	s.Mu.Unlock()
	if it == nil || it.Deleted {
		w.WriteHeader(404)
		return
	}
	data := it.Data
	if rg := r.Header.Get("Range"); rg != "" {
		var a, b int64 = 0, int64(len(data)) - 1
		if strings.HasPrefix(rg, "bytes=-") { // O2 gets suffix ranges wrong
			n, _ := strconv.ParseInt(rg[7:], 10, 64)
			a, b = 0, n
		} else {
			fmt.Sscanf(rg, "bytes=%d-%d", &a, &b)
		}
		b = min(b, int64(len(data))-1)
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", a, b, len(data)))
		w.WriteHeader(206)
		_, _ = w.Write(data[a : b+1])
		return
	}
	w.WriteHeader(200)
	_, _ = w.Write(data)
}
