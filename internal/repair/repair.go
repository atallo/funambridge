// Package repair finds and repairs duplicate copies left on O2 by failed
// uploads of older versions (before 0.1.10): the same name twice, or
// "name (N).ext" next to "name.ext".
//
// For every group it downloads every copy and checks it is complete (real
// length == size) and identical (MD5):
//   - all complete copies identical -> keep one under the real name, trash the rest;
//   - only a "name (N)" copy is complete -> upload it under the real name, trash the rest;
//   - complete copies that differ -> nothing is touched, reported for review.
//
// Without apply nothing is changed (dry run).
package repair

import (
	"context"
	"crypto/md5"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/atallo/funambridge/internal/sapi"
)

var variantRe = regexp.MustCompile(`^(.*) \((\d+)\)(\.[^.]*)?$`)

// Canonical: "a.bucket (2).3" -> "a.bucket.3".
func Canonical(name string) string {
	if m := variantRe.FindStringSubmatch(name); m != nil {
		return m[1] + m[3]
	}
	return name
}

type copyInfo struct {
	sapi.Media
	got int64
	md5 string
	ok  bool
	err error
}

type Summary struct {
	Folders, Files, Groups, Fixed, Problems int
	Extra                                   int64
}

func gb(n int64) string {
	if n >= 1e8 {
		return fmt.Sprintf("%.2f GB", float64(n)/1e9)
	}
	return fmt.Sprintf("%.1f MB", float64(n)/1e6)
}

func when(ms int64) string {
	if ms <= 0 {
		return "?"
	}
	return time.UnixMilli(ms).Format("2006-01-02 15:04")
}

func digest(ctx context.Context, c *sapi.Client, id string, keep io.Writer) (int64, string, error) {
	rc, err := c.Open(ctx, id, 0, -1)
	if err != nil {
		return 0, "", err
	}
	defer rc.Close()
	h := md5.New()
	w := io.Writer(h)
	if keep != nil {
		w = io.MultiWriter(h, keep)
	}
	n, err := io.Copy(w, rc)
	return n, hex.EncodeToString(h.Sum(nil)), err
}

// Run scans root (a path like "backup/NAS.hbk") recursively.
func Run(ctx context.Context, c *sapi.Client, root string, apply bool, out func(string)) (Summary, error) {
	var sum Summary
	all, err := c.AllFolders(ctx)
	if err != nil {
		return sum, err
	}
	rootID, err := c.RootID(ctx)
	if err != nil {
		return sum, err
	}
	kids := map[string][]sapi.Folder{}
	for _, f := range all {
		kids[f.ParentID] = append(kids[f.ParentID], f)
	}
	cur := rootID
	for _, part := range strings.Split(strings.Trim(root, "/"), "/") {
		if part == "" {
			continue
		}
		found := ""
		for _, f := range kids[cur] {
			if f.Name == part {
				found = f.ID
			}
		}
		if found == "" {
			return sum, fmt.Errorf("no existe la carpeta %s", root)
		}
		cur = found
	}
	type group struct {
		path, fid, canon string
		copies           []sapi.Media
	}
	var todo []group
	var walk func(fid, p string) error
	walk = func(fid, p string) error {
		ms, err := c.ListMedia(ctx, fid)
		if err != nil {
			return err
		}
		sum.Folders++
		sum.Files += len(ms)
		byName := map[string][]sapi.Media{}
		for _, m := range ms {
			byName[m.Name] = append(byName[m.Name], m)
		}
		groups := map[string][]sapi.Media{}
		for n, list := range byName {
			cn := Canonical(n)
			if cn != n && byName[cn] == nil {
				for _, m := range list {
					out(fmt.Sprintf("  ? %s/%s: copia '(N)' sin el nombre original (no se toca)", p, m.Name))
				}
				continue
			}
			groups[cn] = append(groups[cn], list...)
		}
		names := make([]string, 0, len(groups))
		for n, list := range groups {
			if len(list) > 1 {
				names = append(names, n)
			}
		}
		sort.Strings(names)
		for _, n := range names {
			todo = append(todo, group{p, fid, n, groups[n]})
		}
		sub := kids[fid]
		sort.Slice(sub, func(i, j int) bool { return sub[i].Name < sub[j].Name })
		for _, f := range sub {
			if err := walk(f.ID, p+"/"+f.Name); err != nil {
				return err
			}
		}
		return nil
	}
	if err := walk(cur, strings.Trim(root, "/")); err != nil {
		return sum, err
	}
	out(fmt.Sprintf("%d carpetas, %d ficheros, %d grupos con copias de más", sum.Folders, sum.Files, len(todo)))

	for _, g := range todo {
		sum.Groups++
		infos := make([]*copyInfo, len(g.copies))
		var wg sync.WaitGroup
		sem := make(chan struct{}, 4)
		for i, m := range g.copies {
			infos[i] = &copyInfo{Media: m}
			wg.Add(1)
			go func(ci *copyInfo) {
				defer wg.Done()
				sem <- struct{}{}
				defer func() { <-sem }()
				ci.got, ci.md5, ci.err = digest(ctx, c, ci.ID, nil)
				ci.ok = ci.err == nil && ci.got == ci.Size
			}(infos[i])
		}
		wg.Wait()
		sort.Slice(infos, func(i, j int) bool { return infos[i].Modified < infos[j].Modified })
		var good []*copyInfo
		md5s := map[string]bool{}
		for _, ci := range infos {
			if ci.ok {
				good = append(good, ci)
				md5s[ci.md5] = true
			}
		}
		var keep, reupload *copyInfo
		problem := ""
		switch {
		case len(good) == 0:
			problem = "ninguna copia está completa"
		case len(md5s) > 1:
			problem = "hay copias completas con contenido distinto"
		default:
			for _, ci := range good {
				if ci.Name == g.canon {
					keep = ci // newest exact-name copy (sorted by date)
				}
			}
			if keep == nil {
				reupload = good[len(good)-1]
			}
		}
		out("\n" + g.path + "/" + g.canon)
		var del []*copyInfo
		for _, ci := range infos {
			tag := "borrar"
			switch {
			case problem != "":
				tag = "-"
			case ci == keep:
				tag = "CONSERVAR"
			case ci == reupload:
				tag = "resubir como original, luego borrar"
			}
			if problem == "" && ci != keep {
				del = append(del, ci)
			}
			state := "completa"
			if ci.err != nil {
				state = "ERROR (" + ci.err.Error() + ")"
			} else if !ci.ok {
				state = fmt.Sprintf("INCOMPLETA (%d de %d B)", ci.got, ci.Size)
			}
			out(fmt.Sprintf("    %-34s %9s  %s  md5 %.12s  %-10s -> %s", ci.Name, gb(ci.Size), when(ci.Modified), ci.md5, state, tag))
		}
		if problem != "" {
			sum.Problems++
			out("    !! " + problem + ": no se toca")
			continue
		}
		for _, ci := range del {
			sum.Extra += ci.Size
		}
		if !apply {
			continue
		}
		if err := applyGroup(ctx, c, g.fid, g.canon, reupload, del); err != nil {
			sum.Problems++
			out("    !! falló la reparación: " + err.Error())
			continue
		}
		sum.Fixed++
		out("    reparado")
	}
	msg := fmt.Sprintf("\nGrupos: %d, con problemas: %d, espacio sobrante: %s", sum.Groups, sum.Problems, gb(sum.Extra))
	if apply {
		msg += fmt.Sprintf(", reparados: %d", sum.Fixed)
	} else {
		msg += "  (simulación: no se ha cambiado nada; usa --apply)"
	}
	out(msg)
	return sum, nil
}

func applyGroup(ctx context.Context, c *sapi.Client, fid, canon string, src *copyInfo, del []*copyInfo) error {
	if src != nil {
		f, err := os.CreateTemp("", "funambridge-fix-*")
		if err != nil {
			return err
		}
		defer os.Remove(f.Name())
		defer f.Close()
		n, sum, err := digest(ctx, c, src.ID, f)
		if err != nil || n != src.Size || sum != src.md5 {
			return fmt.Errorf("%s: la copia cambió al releerla", src.Name)
		}
		for _, ci := range del { // free the real name first
			if ci.Name == canon {
				if err := c.DeleteMedia(ctx, ci.ID); err != nil {
					return err
				}
			}
		}
		if _, err := c.Upload(ctx, sapi.UploadReq{FolderID: fid, Name: canon, Size: n,
			Open: func(off int64) (io.Reader, error) { return io.NewSectionReader(f, off, n-off), nil }}); err != nil {
			return err
		}
	}
	for _, ci := range del {
		if src != nil && ci.Name == canon {
			continue
		}
		if err := c.DeleteMedia(ctx, ci.ID); err != nil {
			return err
		}
	}
	return nil
}
