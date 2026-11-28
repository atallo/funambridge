//go:build live

// Live test against a real O2 account. Never runs in a normal `go test`:
//
//	FUNAMBRIDGE_LIVE_CONFIG=/ruta/config.yaml FUNAMBRIDGE_LIVE_ACCOUNT=prueba \
//	  go test -tags live -run Live -v ./internal/sapi/
//
// It only makes normal requests with the session's real validation key (a
// fake key invalidates the whole session!) and works inside a throwaway folder
// "funambridge-pruebas-go" that it deletes at the end. It never writes the
// config file.
package sapi

import (
	"bytes"
	"context"
	"crypto/rand"
	"io"
	"os"
	"testing"
	"time"

	"github.com/atallo/funambridge/internal/config"
)

func liveClient(t *testing.T) *Client {
	p := os.Getenv("FUNAMBRIDGE_LIVE_CONFIG")
	if p == "" {
		t.Skip("FUNAMBRIDGE_LIVE_CONFIG no definido")
	}
	cfg, err := config.Load(p)
	if err != nil {
		t.Fatal(err)
	}
	a := cfg.ByName(os.Getenv("FUNAMBRIDGE_LIVE_ACCOUNT"))
	if a == nil {
		t.Fatal("cuenta no encontrada")
	}
	sess := a.Session // a copy: rotations stay in memory
	return New(a.BaseURL, a.DeviceID, &sess, nil)
}

func rnd(n int) []byte { b := make([]byte, n); _, _ = rand.Read(b); return b }

func TestLive(t *testing.T) {
	c := liveClient(t)
	ctx := context.Background()
	if err := c.Profile(ctx); err != nil {
		t.Fatalf("profile: %v", err)
	}
	t.Logf("sesión OK con huella %s", FingerprintName())
	lim := c.Limits(ctx)
	t.Logf("límites: %+v, subida: %s", lim, c.UploadBase(ctx))
	st, err := c.Storage(ctx)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("cuota: usado %d de %d (libre %d, papelera %d)", st.Used, st.Quota, st.Free, st.Trash)
	all, err := c.AllFolders(ctx)
	if err != nil {
		t.Fatal(err)
	}
	root, _ := c.RootID(ctx)
	t.Logf("%d carpetas", len(all))

	var fid string
	for _, f := range all {
		if f.ParentID == root && f.Name == "funambridge-pruebas-go" {
			fid = f.ID
		}
	}
	if fid == "" {
		if fid, err = c.CreateFolder(ctx, root, "funambridge-pruebas-go"); err != nil {
			t.Fatal(err)
		}
	}
	defer func() {
		ms, _ := c.ListMedia(ctx, fid)
		for _, m := range ms {
			_ = c.DeleteMedia(ctx, m.ID)
		}
		if err := c.DeleteFolder(ctx, fid); err != nil {
			t.Errorf("borrar carpeta: %v", err)
		}
	}()

	up := func(name string, b []byte, existing *Media) *UploadResult {
		r, err := c.Upload(ctx, UploadReq{FolderID: fid, Name: name, Size: int64(len(b)), Existing: existing,
			Open: func(off int64) (io.Reader, error) { return bytes.NewReader(b[off:]), nil }})
		if err != nil {
			t.Fatalf("upload %s: %v", name, err)
		}
		return r
	}
	a := rnd(700_000)
	r1 := up("a.bin", a, nil)
	t.Logf("subido a.bin id=%s status=%s", r1.ID, r1.Status)
	if !c.WaitValidated(ctx, r1.ID, fid, 2*time.Minute) {
		t.Fatal("no terminó la validación")
	}
	rd, err := c.Open(ctx, r1.ID, 1000, 1999)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := io.ReadAll(rd)
	rd.Close()
	if !bytes.Equal(got, a[1000:2000]) {
		t.Fatalf("rango 1000-1999 distinto (%d B)", len(got))
	}
	rd, _ = c.Open(ctx, r1.ID, 0, -1)
	got, _ = io.ReadAll(rd)
	rd.Close()
	if !bytes.Equal(got, a) {
		t.Fatal("descarga completa distinta")
	}
	t.Log("descargas (rango y completa) OK")

	b := rnd(400_000)
	m := &Media{ID: r1.ID}
	r2 := up("a.bin", b, m)
	if r2.ID != r1.ID {
		t.Fatalf("sobrescribir cambió el id: %s -> %s", r1.ID, r2.ID)
	}
	c.WaitValidated(ctx, r2.ID, fid, 2*time.Minute)
	rd, _ = c.Open(ctx, r2.ID, 0, -1)
	got, _ = io.ReadAll(rd)
	rd.Close()
	if !bytes.Equal(got, b) {
		t.Fatal("tras sobrescribir el contenido no es el nuevo")
	}
	t.Log("sobrescritura en el sitio OK (mismo id, contenido nuevo)")

	if err := c.RenameMedia(ctx, r2.ID, "a.bin", "b.bin", ""); err != nil {
		t.Fatalf("renombrar: %v", err)
	}
	cid, err := c.CopyMedia(ctx, r2.ID, fid, "c.bin")
	if err != nil {
		t.Fatalf("copiar: %v", err)
	}
	t.Logf("renombrar OK, copia en el servidor id=%s", cid)

	r0, err := c.Upload(ctx, UploadReq{FolderID: fid, Name: "vacio.bin", Size: 0,
		Open: func(int64) (io.Reader, error) { return bytes.NewReader(nil), nil }})
	if err != nil {
		t.Logf("fichero vacío con el protocolo de la app: %v; probando multipart", err)
		if r0, err = c.UploadMultipart(ctx, fid, "vacio.bin", nil); err != nil {
			t.Fatalf("fichero vacío: %v", err)
		}
		t.Log("fichero vacío OK con multipart")
	} else {
		t.Logf("fichero vacío OK con el protocolo de la app (id %s)", r0.ID)
	}

	time.Sleep(20 * time.Second)
	ms, err := c.ListMedia(ctx, fid)
	if err != nil {
		t.Fatal(err)
	}
	names := map[string]int64{}
	for _, m := range ms {
		names[m.Name] = m.Size
	}
	t.Logf("contenido final: %v", names)
	if names["b.bin"] != 400_000 || names["c.bin"] != 400_000 || len(names) != 3 {
		t.Fatalf("contenido inesperado: %v", names)
	}
}
