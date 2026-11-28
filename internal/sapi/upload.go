package sapi

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"mime"
	"path"
	"strconv"
	"strings"
	"time"
)

// Upload protocol of the O2 Windows app (works with the web session too,
// tested 28-nov-2026):
//
//  1. POST {web}/sapi/upload/<kind>?action=save-metadata
//     {"data":{contenttype,creationdate,folderid,modificationdate,name,size[,id]}}
//     -> {"id": ...}. With "id" it overwrites that item IN PLACE (same id),
//     so a re-upload never leaves "name (1).ext" behind.
//  2. POST {upload}/sapi/upload/<kind>?action=save&lastupdate=true&acceptasynchronous=true
//     raw body, x-funambol-id, x-funambol-file-size -> {"id","status":"V","etag"}
//  3. If the body is cut, ask how much O2 has:
//     GET {web}/sapi/upload/<kind>?action=save... content-range: bytes */<total>
//     -> 308 + "Range: 0-<last byte>"; then POST only the rest with
//     content-range: bytes <from>-<total-1>/<total>.
//
// After it, O2 validates the file in the background (status V) for a few
// seconds; see Settler.

// UploadReq describes one upload. Open must return a reader positioned at
// offset (the PUT body is spooled to disk, so it can be re-read).
type UploadReq struct {
	FolderID string
	Name     string
	Size     int64
	Open     func(offset int64) (io.Reader, error)
	Existing *Media    // overwrite this item in place
	ModTime  time.Time // zero = now
	Attempts int       // content attempts (default 8)
	Progress func(sent int64)
	Phase    func(string)
}

type UploadResult struct {
	ID, ETag, Status string
}

// kindFor is the SAPI media kind O2 expects for a name.
func kindFor(name string) string {
	switch strings.TrimPrefix(strings.ToLower(path.Ext(name)), ".") {
	case "mp3", "m4a", "wav":
		return "audio"
	case "mp4":
		return "video"
	case "jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "heic":
		return "picture"
	}
	return "file"
}

func kindsFor(names ...string) []string {
	seen, out := map[string]bool{}, []string{}
	for _, n := range append(names, "file") {
		k := n
		if k != "file" {
			k = kindFor(n)
		}
		if !seen[k] {
			seen[k] = true
			out = append(out, k)
		}
	}
	return out
}

func contentType(name, kind string) string {
	if kind == "file" {
		return "application/octet-stream"
	}
	if t := mime.TypeByExtension(strings.ToLower(path.Ext(name))); t != "" {
		return strings.SplitN(t, ";", 2)[0]
	}
	return "application/octet-stream"
}

func o2Date(t time.Time) string { return t.UTC().Format("20060102T150405Z") }

var backoff = []time.Duration{2 * time.Second, 5 * time.Second, 10 * time.Second,
	20 * time.Second, 30 * time.Second, 45 * time.Second, 60 * time.Second}

// Upload sends a file with the Windows-app protocol: resumes cut transfers
// on the same id, overwrites in place, and on final failure retires an item
// it created (never leaves a half file behind).
func (c *Client) Upload(ctx context.Context, u UploadReq) (*UploadResult, error) {
	if u.Existing != nil && u.Size == 0 {
		return nil, ErrEmptyOverwrite
	}
	if u.Attempts <= 0 {
		u.Attempts = 8
	}
	kind := "file" // generic file: O2 stores the bytes as they are
	if c.AsMedia != nil && c.AsMedia() {
		kind = kindFor(u.Name)
	}
	res, err := c.uploadKind(ctx, u, kind)
	if errors.Is(err, ErrUnsupported) && kind != "file" {
		// e.g. a ".jpg" that is not really a picture: store it as a plain file
		log.Infof("O2 no admite '%s' como %s (MED-1050); se sube como fichero genérico", u.Name, kind)
		res, err = c.uploadKind(ctx, u, "file")
	}
	return res, err
}

func (c *Client) uploadKind(ctx context.Context, u UploadReq, kind string) (*UploadResult, error) {
	phase := func(s string) {
		if u.Phase != nil {
			u.Phase(s)
		}
	}
	now := time.Now()
	mod := u.ModTime
	if mod.IsZero() {
		mod = now
	}
	meta := map[string]any{"contenttype": contentType(u.Name, kind), "modificationdate": o2Date(mod),
		"name": u.Name, "size": u.Size, "folderid": numOrStr(u.FolderID)}
	if u.Existing != nil {
		meta["id"] = u.Existing.ID
		created := now
		if u.Existing.Created > 0 {
			created = time.UnixMilli(u.Existing.Created)
		}
		meta["creationdate"] = o2Date(created)
	} else {
		meta["creationdate"] = o2Date(now)
	}

	phase("registrando")
	// Retrying save-metadata is safe even for a NEW file whose answer got
	// lost (tested live, 28-nov-2026): an item registered without content is
	// invisible (not listed, not returned by id), never receives content
	// because we never learnt its id, and does not make O2 rename the next
	// registration of that name to "name (1)".
	var id string
	for attempt := 0; ; attempt++ {
		r, err := c.Do(ctx, Req{Method: "POST", Retry: true,
			Path:   "/sapi/upload/" + kind + "?action=save-metadata&responsetime=true&lastupdate=true",
			Header: map[string]string{"Content-Type": "application/octet-stream"},
			Body:   jbody(map[string]any{"data": meta})})
		if err == nil {
			d := fields(r)
			if id = str(d["id"]); id == "" && u.Existing != nil {
				id = u.Existing.ID
			}
			if id == "" {
				return nil, fmt.Errorf("save-metadata de '%s': O2 no devolvió id", u.Name)
			}
			break
		}
		if errors.Is(err, ErrValidating) && u.Existing != nil && attempt < 5 {
			// overwriting a file O2 is still processing: wait, like the app
			phase("esperando a O2")
			log.Infof("'%s' aún se está validando en O2 (MED-1017); se espera para sobrescribirlo", u.Name)
			c.WaitValidated(ctx, u.Existing.ID, u.FolderID, 2*time.Minute)
			continue
		}
		return nil, err
	}
	isNew := u.Existing == nil

	res, err := c.sendContent(ctx, u, kind, id, phase)
	if err != nil {
		if isNew && !errors.Is(err, ErrUnsupported) {
			// never leave an item with half its content in the cloud
			if derr := c.DeleteMedia(context.Background(), id); derr == nil {
				log.Infof("retirado el elemento a medias de '%s' (id %s) tras fallar la subida", u.Name, id)
			} else {
				log.Warnf("no se pudo retirar el elemento a medias de '%s' (id %s): %v", u.Name, id, derr)
			}
		} else if isNew {
			_ = c.DeleteMedia(context.Background(), id)
		}
		return nil, err
	}
	if res.ID == "" {
		res.ID = id
	}
	return res, nil
}

func (c *Client) sendContent(ctx context.Context, u UploadReq, kind, id string, phase func(string)) (*UploadResult, error) {
	up := c.UploadBase(ctx)
	savePath := "/sapi/upload/" + kind + "?action=save&lastupdate=true&acceptasynchronous=true"
	var last error
	for attempt := 0; attempt < u.Attempts; attempt++ {
		var offset int64
		if attempt > 0 {
			wait := backoff[min(attempt-1, len(backoff)-1)]
			phase(fmt.Sprintf("reintento %d/%d", attempt+1, u.Attempts))
			log.Warnf("subida de '%s' cortada (%v); reintento %d/%d en %s", u.Name, last, attempt+1, u.Attempts, wait)
			select {
			case <-time.After(wait):
			case <-ctx.Done():
				return nil, fmt.Errorf("el cliente abandonó la subida: %w", ctx.Err())
			}
			got, done, err := c.queryOffset(ctx, kind, id, u.Size, savePath)
			switch {
			case err != nil:
				log.Debugf("consulta de offset de '%s': %v", u.Name, err)
			case done != nil:
				log.Infof("O2 ya tenía completo '%s' (id %s)", u.Name, id)
				return done, nil
			case got > 0:
				offset = got
				log.Infof("se reanuda '%s' desde el byte %d de %d", u.Name, offset, u.Size)
			}
		}
		phase("subiendo")
		h := map[string]string{"Content-Type": contentType(u.Name, kind),
			"x-funambol-id": id, "x-funambol-file-size": strconv.FormatInt(u.Size, 10)}
		if offset > 0 {
			h["content-range"] = fmt.Sprintf("bytes %d-%d/%d", offset, u.Size-1, u.Size)
		}
		remaining := u.Size - offset
		r, err := c.Do(ctx, Req{Method: "POST", Base: up, Path: savePath, Upload: true, Header: h,
			Size: remaining, BodyFn: func() (io.Reader, error) {
				rd, err := u.Open(offset)
				if err != nil {
					return nil, err
				}
				rd = io.LimitReader(rd, remaining)
				if u.Progress != nil {
					rd = &progressReader{r: rd, base: offset, f: u.Progress}
				}
				return rd, nil
			}})
		if err == nil {
			d := fields(r)
			return &UploadResult{ID: str(d["id"]), ETag: str(d["etag"]), Status: str(d["status"])}, nil
		}
		last = err
		if ctx.Err() != nil {
			return nil, fmt.Errorf("el cliente abandonó la subida: %w", ctx.Err())
		}
		switch {
		case errors.Is(err, ErrTooLarge), errors.Is(err, ErrUnsupported):
			return nil, err
		case errors.Is(err, ErrValidating):
			c.WaitValidated(ctx, id, u.FolderID, 2*time.Minute)
		case IsSession(err):
			return nil, err
		}
		k, _ := KindOf(err)
		if k == KindAPI && !errors.Is(err, ErrValidating) {
			return nil, err // O2 rejected it: not a cut, do not resume
		}
	}
	return nil, last
}

// queryOffset asks O2 how many bytes of an interrupted upload it has.
func (c *Client) queryOffset(ctx context.Context, kind, id string, size int64, savePath string) (int64, *UploadResult, error) {
	r, err := c.Do(ctx, Req{Method: "GET", Path: savePath, Header: map[string]string{
		"content-range": fmt.Sprintf("bytes */%d", size), "x-funambol-id": id,
		"x-funambol-file-size": strconv.FormatInt(size, 10)}})
	if err != nil {
		return 0, nil, err
	}
	switch r.Status {
	case 308:
		return parseReceived(r.Header.Get("Range"), size), nil, nil
	case 200, 201:
		if d := fields(r); str(d["etag"]) != "" || str(d["status"]) != "" {
			return size, &UploadResult{ID: str(d["id"]), ETag: str(d["etag"]), Status: str(d["status"])}, nil
		}
	}
	return 0, nil, nil
}

// parseReceived reads "Range: 0-N" (last byte received).
func parseReceived(v string, size int64) int64 {
	v = strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(v), "bytes="))
	i := strings.LastIndex(v, "-")
	if v == "" || i < 0 {
		return 0
	}
	last, err := strconv.ParseInt(strings.TrimSpace(v[i+1:]), 10, 64)
	if err != nil || last < 0 || last+1 > size {
		return 0
	}
	if last+1 == size && size > 0 {
		return size - 1 // O2 has it all but did not close it: resend the last byte
	}
	return last + 1
}

type progressReader struct {
	r    io.Reader
	base int64
	n    int64
	f    func(int64)
}

func (p *progressReader) Read(b []byte) (int, error) {
	n, err := p.r.Read(b)
	p.n += int64(n)
	p.f(p.base + p.n)
	return n, err
}

// UploadMultipart is the web client's one-shot upload (upload host, one
// multipart POST). Used for empty files, which the app protocol cannot send.
func (c *Client) UploadMultipart(ctx context.Context, folderID, name string, body []byte) (*UploadResult, error) {
	meta := jbody(map[string]any{"data": map[string]any{"name": name, "size": len(body),
		"modificationdate": "", "folderid": numOrStr(folderID)}})
	boundary := "----funambridge" + reqID()
	fname := strings.NewReplacer("\r", " ", "\n", " ", `"`, "%22").Replace(name)
	var buf bytes.Buffer
	fmt.Fprintf(&buf, "--%s\r\nContent-Disposition: form-data; name=\"data\"\r\n\r\n%s\r\n", boundary, meta)
	fmt.Fprintf(&buf, "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"+
		"Content-Type: application/octet-stream\r\n\r\n", boundary, fname)
	buf.Write(body)
	fmt.Fprintf(&buf, "\r\n--%s--\r\n", boundary)
	r, err := c.Do(ctx, Req{Method: "POST", Base: c.UploadBase(ctx), Path: "/sapi/upload?action=save",
		Upload: true, Body: buf.Bytes(),
		Header: map[string]string{"Content-Type": "multipart/form-data; boundary=" + boundary}})
	if err != nil {
		return nil, err
	}
	d := fields(r)
	if str(d["id"]) == "" {
		return nil, fmt.Errorf("subida de '%s': O2 no devolvió id", name)
	}
	return &UploadResult{ID: str(d["id"]), ETag: str(d["etag"]), Status: str(d["status"])}, nil
}

// SetBackoffForTests shortens the waits between upload attempts.
func SetBackoffForTests(d time.Duration) {
	for i := range backoff {
		backoff[i] = d
	}
}
