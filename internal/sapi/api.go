package sapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

var jsonHdr = map[string]string{"Content-Type": "application/json;charset=UTF-8"}

func jbody(v any) []byte { b, _ := json.Marshal(v); return b }

// ---- system information (public) ----------------------------------------

// Info returns /sapi/system/information, cached for 6 h (on failure the last
// copy, or an empty map: callers use defaults).
func (c *Client) Info(ctx context.Context) map[string]any {
	c.infoMu.Lock()
	defer c.infoMu.Unlock()
	if c.info != nil && time.Since(c.infoAt) < 6*time.Hour {
		return c.info
	}
	r, err := c.Do(ctx, Req{Method: "GET", Path: "/sapi/system/information?action=get",
		NoKey: true, Retry: true})
	if err == nil {
		var d map[string]any
		if json.Unmarshal(r.Body, &d) == nil {
			c.info, c.infoAt = d, time.Now()
			return d
		}
	}
	log.Debugf("system/information: %v", err)
	c.infoAt = time.Now().Add(-6*time.Hour + 5*time.Minute) // retry in 5 min
	if c.info == nil {
		return map[string]any{}
	}
	return c.info
}

type Limits struct {
	MaxUpload int64 `json:"max_upload"` // bytes
	FolderOps int   `json:"folder_ops"` // items per folder delete/move
	TrashDays int   `json:"trash_days"`
}

func (c *Client) Limits(ctx context.Context) Limits {
	d := c.Info(ctx)
	get := func(k string, def int64) int64 {
		if v, ok := d[k]; ok {
			if n := num(v); n > 0 {
				return n
			}
			var n int64
			if _, err := fmt.Sscan(str(v), &n); err == nil && n > 0 {
				return n
			}
		}
		return def
	}
	return Limits{
		MaxUpload: get("sapi.upload.max-size-in-mb", 4096) << 20,
		FolderOps: int(get("folders.softdelete-updates-limit", 1000)),
		TrashDays: int(get("trash.cleanup.items-expire-time-in-minutes", 86400) / 1440),
	}
}

// UploadBase is the upload host announced by O2 ("sapi.upload.endpoint"),
// else upload.<web host>.
func (c *Client) UploadBase(ctx context.Context) string {
	if ep := strings.TrimRight(str(c.Info(ctx)["sapi.upload.endpoint"]), "/"); strings.HasPrefix(ep, "https://") ||
		(strings.HasPrefix(ep, "http://") && strings.HasPrefix(c.Base, "http://")) {
		return ep
	}
	return strings.Replace(c.Base, "://", "://upload.", 1)
}

// ---- session -------------------------------------------------------------

// Profile is an authenticated no-op: checks the session and keeps it alive.
func (c *Client) Profile(ctx context.Context) error {
	_, err := c.Do(ctx, Req{Method: "GET", Path: "/sapi/profile?action=get"})
	return err
}

// ---- folders -------------------------------------------------------------

type Folder struct {
	ID, Name, ParentID string
}

func (c *Client) RootID(ctx context.Context) (string, error) {
	c.rootMu.Lock()
	defer c.rootMu.Unlock()
	if c.rootID != "" {
		return c.rootID, nil
	}
	r, err := c.Do(ctx, Req{Method: "GET", Path: "/sapi/media/folder/root?action=get", Retry: true})
	if err != nil {
		return "", err
	}
	var d struct {
		Folders []map[string]any `json:"folders"`
	}
	if err := data(r, &d); err != nil || len(d.Folders) == 0 {
		return "", fmt.Errorf("carpeta raíz: %v", err)
	}
	c.rootID = str(d.Folders[0]["id"])
	return c.rootID, nil
}

// AllFolders lists every folder of the account in one call: O2 ignores
// parentid and always returns the whole flat list.
func (c *Client) AllFolders(ctx context.Context) ([]Folder, error) {
	root, err := c.RootID(ctx)
	if err != nil {
		return nil, err
	}
	r, err := c.Do(ctx, Req{Method: "GET", Path: "/sapi/media/folder?action=get&parentid=" + root, Retry: true})
	if err != nil {
		return nil, err
	}
	var d struct {
		Folders []map[string]any `json:"folders"`
	}
	if err := data(r, &d); err != nil {
		return nil, err
	}
	out := make([]Folder, 0, len(d.Folders))
	for _, f := range d.Folders {
		out = append(out, Folder{ID: str(f["id"]), Name: str(f["name"]), ParentID: str(f["parentid"])})
	}
	return out, nil
}

func (c *Client) CreateFolder(ctx context.Context, parentID, name string) (string, error) {
	r, err := c.Do(ctx, Req{Method: "POST", Path: "/sapi/media/folder?action=save",
		Body:   jbody(map[string]any{"data": map[string]any{"name": name, "parentid": numOrStr(parentID)}}),
		Header: jsonHdr})
	if err != nil {
		return "", err
	}
	d := fields(r)
	if id := str(d["id"]); id != "" {
		return id, nil
	}
	if f, ok := d["folder"].(map[string]any); ok && str(f["id"]) != "" {
		return str(f["id"]), nil
	}
	if fs, ok := d["folders"].([]any); ok && len(fs) > 0 {
		if f, ok := fs[0].(map[string]any); ok && str(f["id"]) != "" {
			return str(f["id"]), nil
		}
	}
	all, err := c.AllFolders(ctx) // no id in any known shape: look it up
	if err == nil {
		for _, f := range all {
			if f.ParentID == parentID && f.Name == name {
				return f.ID, nil
			}
		}
	}
	return "", fmt.Errorf("crear carpeta %q: O2 no devolvió el id", name)
}

// UpdateFolder renames and/or moves a folder (parentID "" = same parent).
func (c *Client) UpdateFolder(ctx context.Context, id, name, parentID string) error {
	d := map[string]any{"id": numOrStr(id), "name": name}
	if parentID != "" {
		d["parentid"] = numOrStr(parentID)
	}
	_, err := c.Do(ctx, Req{Method: "POST", Path: "/sapi/media/folder?action=save",
		Body: jbody(map[string]any{"data": d}), Header: jsonHdr})
	return err
}

// DeleteFolder soft-deletes (to the trash). ErrFolderTooBig if it holds more
// items than O2 lets delete at once.
func (c *Client) DeleteFolder(ctx context.Context, id string) error {
	_, err := c.Do(ctx, Req{Method: "GET", Path: "/sapi/media/folder?action=softdelete&id=" + id})
	return err
}

// ---- media -----------------------------------------------------------------

type Media struct {
	ID       string
	Name     string
	Size     int64
	Created  int64 // ms
	Modified int64 // ms
	ETag     string
	Status   string
	URL      string
}

func mediaFrom(m map[string]any) Media {
	return Media{ID: str(m["id"]), Name: str(m["name"]), Size: num(m["size"]),
		Created: num(m["creationdate"]), Modified: num(m["modificationdate"]),
		ETag: str(m["etag"]), Status: str(m["status"]), URL: str(m["url"])}
}

var mediaFields = []string{"name", "size", "modificationdate", "creationdate", "etag"}

// ListMedia lists a folder's files (paginated by 200).
func (c *Client) ListMedia(ctx context.Context, folderID string) ([]Media, error) {
	var out []Media
	body := jbody(map[string]any{"data": map[string]any{"fields": mediaFields}})
	for off := 0; ; {
		r, err := c.Do(ctx, Req{Method: "POST", Retry: true, Header: jsonHdr, Body: body,
			Path: fmt.Sprintf("/sapi/media?action=get&folderid=%s&limit=200&offset=%d", folderID, off)})
		if err != nil {
			return nil, err
		}
		var d struct {
			Media []map[string]any `json:"media"`
			More  bool             `json:"more"`
		}
		if err := data(r, &d); err != nil {
			return nil, err
		}
		for _, m := range d.Media {
			out = append(out, mediaFrom(m))
		}
		if !d.More || len(d.Media) == 0 {
			return out, nil
		}
		off += len(d.Media)
	}
}

// GetMedia reads one item (nil, nil if O2 does not know the id).
func (c *Client) GetMedia(ctx context.Context, id string, fields ...string) (*Media, error) {
	if len(fields) == 0 {
		fields = []string{"name", "url", "size", "etag", "creationdate", "modificationdate", "folderid"}
	}
	r, err := c.Do(ctx, Req{Method: "POST", Path: "/sapi/media?action=get", Retry: true, Header: jsonHdr,
		Body: jbody(map[string]any{"data": map[string]any{"ids": []any{numOrStr(id)}, "fields": fields}})})
	if err != nil {
		return nil, err
	}
	var d struct {
		Media []map[string]any `json:"media"`
	}
	if err := data(r, &d); err != nil {
		return nil, err
	}
	for _, m := range d.Media {
		if str(m["id"]) == id {
			mm := mediaFrom(m)
			return &mm, nil
		}
	}
	return nil, nil
}

func (c *Client) DeleteMedia(ctx context.Context, id string) error {
	_, err := c.Do(ctx, Req{Method: "GET", Path: "/sapi/media?action=softdelete&id=" + id})
	c.urlMu.Lock()
	delete(c.urls, id)
	c.urlMu.Unlock()
	return err
}

type Storage struct {
	Quota int64 `json:"quota"`
	Used  int64 `json:"used"`
	Free  int64 `json:"free"`
	Trash int64 `json:"trash"`
}

func (c *Client) Storage(ctx context.Context) (Storage, error) {
	r, err := c.Do(ctx, Req{Method: "GET", Path: "/sapi/media?action=get-storage-space&softdeleted=true", Retry: true})
	if err != nil {
		return Storage{}, err
	}
	var d map[string]any
	if err := data(r, &d); err != nil {
		return Storage{}, err
	}
	s := Storage{Quota: num(d["quota"]), Used: num(d["used"]), Free: num(d["free"]), Trash: num(d["softdeleted"])}
	if _, ok := d["free"]; !ok && s.Quota > 0 {
		s.Free = s.Quota - s.Used
	}
	if s.Free < 0 {
		s.Free = 0
	}
	return s, nil
}

// ValidationStatus: "V"/"A" while O2 still processes a fresh upload, another
// letter ("U"...) once done. ("", nil) if O2 no longer knows the id.
func (c *Client) ValidationStatus(ctx context.Context, id, folderID string) (string, error) {
	entry := map[string]any{"id": numOrStr(id)}
	if folderID != "" {
		entry["folder_id"] = numOrStr(folderID)
	}
	r, err := c.Do(ctx, Req{Method: "POST", Path: "/sapi/media?action=get-validation-status",
		Header: jsonHdr, Retry: true, Body: jbody(map[string]any{"data": map[string]any{"ids": []any{entry}}})})
	if err != nil {
		return "", err
	}
	var d struct {
		IDs []map[string]any `json:"ids"`
	}
	if err := data(r, &d); err != nil {
		return "", err
	}
	for _, it := range d.IDs {
		if str(it["id"]) == id {
			return str(it["status"]), nil
		}
	}
	return "", nil
}

// Validating reports whether a status means "still being processed".
func Validating(st string) bool { return st == "V" || st == "A" }

// ---- rename / move / copy ------------------------------------------------

// RenameMedia renames and/or moves a file (folderID "" = same folder), like
// the Windows app: save-metadata with the item's id.
func (c *Client) RenameMedia(ctx context.Context, id, oldName, newName, folderID string) error {
	d := map[string]any{"id": id, "name": newName}
	if folderID != "" {
		d["folderid"] = numOrStr(folderID)
	}
	var last error
	for _, kind := range kindsFor(oldName, newName) {
		for attempt := 0; attempt < 6; attempt++ {
			_, err := c.Do(ctx, Req{Method: "POST", Header: jsonHdr, Body: jbody(map[string]any{"data": d}),
				Path: "/sapi/upload/" + kind + "?action=save-metadata"})
			if err == nil {
				return nil
			}
			last = err
			if !errors.Is(err, ErrValidating) {
				break
			}
			c.WaitValidated(ctx, id, "", 2*time.Minute)
		}
		if errors.Is(last, ErrUnsupported) {
			break
		}
	}
	return last
}

// CopyMedia copies a file inside the cloud (no download/upload) and returns
// the new id.
func (c *Client) CopyMedia(ctx context.Context, id, parentID, name string) (string, error) {
	d := map[string]any{"id": numOrStr(id), "parentid": numOrStr(parentID)}
	if name != "" {
		d["name"] = name
	}
	r, err := c.Do(ctx, Req{Method: "POST", Path: "/sapi/media/file?action=copy", Header: jsonHdr,
		Body: jbody(map[string]any{"data": d})})
	if err != nil {
		return "", err
	}
	m := fields(r)
	if s := str(m["id"]); s != "" {
		return s, nil
	}
	for _, k := range []string{"media", "file"} {
		switch v := m[k].(type) {
		case map[string]any:
			if s := str(v["id"]); s != "" {
				return s, nil
			}
		case []any:
			if len(v) > 0 {
				if mm, ok := v[0].(map[string]any); ok && str(mm["id"]) != "" {
					return str(mm["id"]), nil
				}
			}
		}
	}
	return "", nil
}

// WaitValidated polls ValidationStatus until O2 finished processing id (or
// max elapsed / ctx done). true if it is done.
func (c *Client) WaitValidated(ctx context.Context, id, folderID string, max time.Duration) bool {
	deadline := time.Now().Add(max)
	for {
		st, err := c.ValidationStatus(ctx, id, folderID)
		if err == nil && !Validating(st) {
			return true
		}
		if time.Now().After(deadline) || ctx.Err() != nil {
			return false
		}
		select {
		case <-time.After(5 * time.Second):
		case <-ctx.Done():
			return false
		}
	}
}

// ---- downloads -------------------------------------------------------------

const urlTTL = 20 * time.Minute

func (c *Client) signedURL(ctx context.Context, id string, fresh bool) (string, error) {
	c.urlMu.Lock()
	if h, ok := c.urls[id]; ok && !fresh && time.Since(h.at) < urlTTL {
		c.urlMu.Unlock()
		return h.url, nil
	}
	c.urlMu.Unlock()
	m, err := c.GetMedia(ctx, id, "url")
	if err != nil {
		return "", err
	}
	if m == nil || m.URL == "" {
		return "", &Error{Op: "download " + id, Kind: KindNotFound, Msg: "O2 no da URL de descarga"}
	}
	u := m.URL
	if strings.HasPrefix(u, "/") {
		u = c.Base + u
	}
	c.urlMu.Lock()
	if len(c.urls) > 4000 {
		c.urls = map[string]urlHit{}
	}
	c.urls[id] = urlHit{u, time.Now()}
	c.urlMu.Unlock()
	return u, nil
}

// Open streams bytes [start, end] (inclusive; end < 0 = to the end) of a file.
// O2 honours absolute ranges with 206; suffix ranges ("bytes=-N") it gets
// wrong, so callers always pass absolute offsets. If O2 ignores the range
// (200), the skipped bytes are discarded here.
func (c *Client) Open(ctx context.Context, id string, start, end int64) (io.ReadCloser, error) {
	for attempt := 0; attempt < 2; attempt++ {
		u, err := c.signedURL(ctx, id, attempt > 0)
		if err != nil {
			return nil, err
		}
		h := map[string]string{}
		if start > 0 || end >= 0 {
			rng := fmt.Sprintf("bytes=%d-", start)
			if end >= 0 {
				rng += fmt.Sprint(end)
			}
			h["Range"] = rng
		}
		r, err := c.Do(ctx, Req{Method: "GET", Base: u, Path: "", NoKey: true, Header: h, Stream: true})
		if err != nil {
			k, _ := KindOf(err)
			if attempt == 0 && (k == KindSession || k == KindEdge || k == KindNotFound) {
				log.Debugf("descarga %s: %v con la URL guardada; se pide otra", id, err)
				continue
			}
			return nil, err
		}
		body := withIdleTimeout(r.Stream, DownloadIdle)
		if r.Status == http.StatusOK && start > 0 {
			if _, err := io.CopyN(io.Discard, body, start); err != nil {
				body.Close()
				return nil, &Error{Op: "download " + id, Kind: KindTransient, Err: err}
			}
		}
		return body, nil
	}
	return nil, &Error{Op: "download " + id, Kind: KindTransient}
}

// numOrStr sends numeric ids as JSON numbers (what the web/app do).
func numOrStr(id string) any {
	var n int64
	if _, err := fmt.Sscan(id, &n); err == nil && fmt.Sprint(n) == id {
		return n
	}
	return id
}
