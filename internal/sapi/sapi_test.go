package sapi_test

import (
	"bytes"
	"context"
	"crypto/rand"
	"errors"
	"io"
	"testing"
	"time"

	"github.com/atallo/funambridge/internal/config"
	"github.com/atallo/funambridge/internal/fakeo2"
	"github.com/atallo/funambridge/internal/sapi"
)

func setup(t *testing.T) (*fakeo2.Server, *sapi.Client, *config.Session) {
	t.Helper()
	f := fakeo2.New()
	t.Cleanup(f.Close)
	sess := &config.Session{JSessionID: "j", ValidationKey: "k1"}
	c := sapi.New(f.URL, "web-test", sess, nil)
	sapi.SetBackoffForTests(10 * time.Millisecond)
	return f, c, sess
}

func rnd(n int) []byte { b := make([]byte, n); _, _ = rand.Read(b); return b }

func opener(b []byte) func(int64) (io.Reader, error) {
	return func(off int64) (io.Reader, error) { return bytes.NewReader(b[off:]), nil }
}

func TestUploadResumesAfterCut(t *testing.T) {
	f, c, _ := setup(t)
	data := rnd(300_000)
	f.CutAfter = 100_000 // the first body dies after 100 KB
	r, err := c.Upload(context.Background(), sapi.UploadReq{FolderID: "1", Name: "a.bin",
		Size: int64(len(data)), Open: opener(data)})
	if err != nil {
		t.Fatal(err)
	}
	items := f.Files("1")
	if len(items) != 1 || items[0].ID != r.ID || !bytes.Equal(items[0].Data, data) {
		t.Fatalf("contenido tras reanudar: %d items, %d B", len(items), len(items[0].Data))
	}
	if f.Calls["/sapi/upload/file?save"] < 2 {
		t.Fatalf("no se reanudó: %v", f.Calls)
	}
}

func TestUploadRetiresHalfItemOnFailure(t *testing.T) {
	f, c, _ := setup(t)
	f.FailContent = 99
	_, err := c.Upload(context.Background(), sapi.UploadReq{FolderID: "1", Name: "a.bin",
		Size: 10, Open: opener(rnd(10)), Attempts: 2})
	if err == nil {
		t.Fatal("debería fallar")
	}
	if n := len(f.Files("1")); n != 0 {
		t.Fatalf("quedó un elemento a medias (%d)", n)
	}
}

func TestOverwriteKeepsID(t *testing.T) {
	f, c, _ := setup(t)
	id := f.Put("1", "a.bin", []byte("old"), 1)
	r, err := c.Upload(context.Background(), sapi.UploadReq{FolderID: "1", Name: "a.bin", Size: 5,
		Open: opener([]byte("nuevo")), Existing: &sapi.Media{ID: id}})
	if err != nil {
		t.Fatal(err)
	}
	items := f.Files("1")
	if r.ID != id || len(items) != 1 || string(items[0].Data) != "nuevo" {
		t.Fatalf("sobrescritura: id %s->%s, %d items", id, r.ID, len(items))
	}
}

func TestEmptyOverwriteRefused(t *testing.T) {
	_, c, _ := setup(t)
	_, err := c.Upload(context.Background(), sapi.UploadReq{FolderID: "1", Name: "a", Size: 0,
		Open: opener(nil), Existing: &sapi.Media{ID: "5"}})
	if !errors.Is(err, sapi.ErrEmptyOverwrite) {
		t.Fatalf("esperaba ErrEmptyOverwrite, %v", err)
	}
}

func TestKeyRotationIsAdopted(t *testing.T) {
	f, c, sess := setup(t)
	f.RotateNext = true
	if err := c.Profile(context.Background()); err != nil {
		t.Fatal(err)
	}
	if sess.ValidationKey == "k1" || sess.ValidationKey != f.Key {
		t.Fatalf("no se adoptó la clave nueva: %s vs %s", sess.ValidationKey, f.Key)
	}
}

func TestSessionDeadIsSession(t *testing.T) {
	f, c, _ := setup(t)
	f.SessionDead = true
	err := c.Profile(context.Background())
	if !sapi.IsSession(err) {
		t.Fatalf("esperaba error de sesión, %v", err)
	}
}

func TestRangeOpen(t *testing.T) {
	f, c, _ := setup(t)
	data := rnd(5000)
	id := f.Put("1", "a.bin", data, 1)
	rc, err := c.Open(context.Background(), id, 100, 199)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := io.ReadAll(rc)
	rc.Close()
	if !bytes.Equal(got, data[100:200]) {
		t.Fatalf("rango: %d B", len(got))
	}
}

func TestFolderTooBig(t *testing.T) {
	f, c, _ := setup(t)
	f.FolderOp = 1
	d := f.Mkdir("1", "big")
	f.Put(d, "a", []byte("1"), 1)
	f.Put(d, "b", []byte("2"), 1)
	err := c.DeleteFolder(context.Background(), d)
	if !errors.Is(err, sapi.ErrFolderTooBig) {
		t.Fatalf("esperaba ErrFolderTooBig, %v", err)
	}
}

func TestStalledDownloadIsCut(t *testing.T) {
	f, c, _ := setup(t)
	old := sapi.DownloadIdle
	sapi.DownloadIdle = 300 * time.Millisecond
	defer func() { sapi.DownloadIdle = old }()
	id := f.Put("1", "a.bin", rnd(100_000), 1)
	f.StallAfter = 10_000
	rc, err := c.Open(context.Background(), id, 0, -1)
	if err != nil {
		t.Fatal(err)
	}
	defer rc.Close()
	start := time.Now()
	n, err := io.Copy(io.Discard, rc)
	if err == nil || time.Since(start) > 5*time.Second {
		t.Fatalf("la descarga parada no se cortó: %d B, err=%v, %s", n, err, time.Since(start))
	}
}
