// Command acceptance is the end-to-end acceptance harness for reprobuild.
//
// It exercises the acceptance criteria directly:
//
//  1. builds two directory trees with identical content but distinct mtimes,
//     archives each, and compares a full summary (byte equality + sha256);
//  2. covers Unicode paths, empty directories and >100-byte file names;
//  3. archives the same tree with a shuffled directory-readdir lister and
//     proves byte equality;
//  4. verifies symlink escape (relative/absolute/broken) is rejected, both
//     in source materialization and when a fixture creates one;
//  5. round-trips an archive through archive/tar;
//  6. drives the HTTP service end to end (build -> get -> artifact).
//
// Output: human-readable check lines plus a JSON summary at -json-out.
// Exit code is 0 only when every check passes.
package main

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"reprobuild/internal/api"
	"reprobuild/internal/archive"
	"reprobuild/internal/builder"
)

type check struct {
	Name   string `json:"name"`
	Pass   bool   `json:"pass"`
	Detail string `json:"detail,omitempty"`
}

type summary struct {
	GeneratedAt string   `json:"generated_at"`
	TreeA       treeInfo `json:"tree_a"`
	TreeB       treeInfo `json:"tree_b"`
	Checks      []check  `json:"checks"`
	Passed      int      `json:"passed"`
	Failed      int      `json:"failed"`
}

type treeInfo struct {
	Path       string `json:"path"`
	MtimeEpoch int64  `json:"mtime_epoch"`
	Files      int    `json:"files"`
	Dirs       int    `json:"dirs"`
	Symlinks   int    `json:"symlinks"`
}

func main() {
	jsonOut := flag.String("json-out", "", "write JSON summary to this path (required)")
	work := flag.String("work", "", "scratch directory (default: temp dir)")
	flag.Parse()
	if *jsonOut == "" {
		fmt.Fprintln(os.Stderr, "-json-out is required")
		os.Exit(2)
	}

	var sumr summary
	sumr.GeneratedAt = time.Now().UTC().Format(time.RFC3339)

	base := *work
	if base == "" {
		var err error
		base, err = os.MkdirTemp("", "reprobuild-acceptance-")
		if err != nil {
			fatal(err)
		}
		defer os.RemoveAll(base)
	}
	if err := os.MkdirAll(base, 0o755); err != nil {
		fatal(err)
	}
	// The scratch dir is dedicated to this harness; clear artifacts from a
	// previous retained run so repeated runs are reproducible.
	for _, child := range []string{
		"tree-a", "tree-b", "escape-rel", "secret", "escape-abs",
		"escape-broken", "loop", "service",
		"a.tar", "b.tar", "shuffled.tar",
	} {
		_ = os.RemoveAll(filepath.Join(base, child))
	}

	treeA := filepath.Join(base, "tree-a")
	treeB := filepath.Join(base, "tree-b")
	mtA := int64(1_000_000_000) // 2001-09-09
	mtB := int64(1_700_000_000) // 2023-11-14

	buildTree(treeA)
	buildTree(treeB)
	clobberTimes(treeA, time.Unix(mtA, 0))
	clobberTimes(treeB, time.Unix(mtB, 0))
	sumr.TreeA = inspectTree(treeA, mtA)
	sumr.TreeB = inspectTree(treeB, mtB)

	// Check 1: byte-identical archives despite different mtimes.
	tarA := filepath.Join(base, "a.tar")
	tarB := filepath.Join(base, "b.tar")
	pack(treeA, tarA, 0)
	pack(treeB, tarB, 0)
	ba, bb := readFile(tarA), readFile(tarB)
	add(&sumr, "archives byte-identical across different mtimes",
		bytes.Equal(ba, bb),
		fmt.Sprintf("a=%d bytes sha256=%s; b=%d bytes sha256=%s",
			len(ba), shaHex(ba), len(bb), shaHex(bb)))

	// Check 2: same tree with shuffled readdir is identical.
	tarShuf := filepath.Join(base, "shuffled.tar")
	pack(treeA, tarShuf, 99)
	bs := readFile(tarShuf)
	add(&sumr, "archive invariant to directory traversal order",
		bytes.Equal(ba, bs),
		fmt.Sprintf("sorted sha256=%s; shuffled(seed=99) sha256=%s", shaHex(ba), shaHex(bs)))

	// Check 3-5: content coverage.
	names, mtimes := headers(ba)
	add(&sumr, "unicode paths (CJK, latin diacritics, emoji) present",
		containsAll(names,
			"目录/", "目录/sub/", "文件.txt",
			"café/", "café/naïve/", "café/naïve/名前.txt",
			"emoji-😀/", "emoji-😀/🎉.dat"),
		strings.Join(names, ", "))
	add(&sumr, "empty directory stored as entry", contains(names, "empty-dir/"), "")
	add(&sumr, "long file name (>100 bytes) stored (PAX)",
		contains(names, strings.Repeat("λ", 60)+".txt"),
		fmt.Sprintf("name bytes=%d", len(strings.Repeat("λ", 60)+".txt")))

	// Check 6: all mtimes pinned to fixed epoch.
	var nonZero int
	for _, mt := range mtimes {
		if !mt.Equal(time.Unix(0, 0).UTC()) {
			nonZero++
		}
	}
	add(&sumr, "all entry mtimes pinned to 1970-01-01 UTC", nonZero == 0,
		fmt.Sprintf("entries=%d non_zero_mtimes=%d", len(mtimes), nonZero))

	// Check 7: entries are sorted in archive order.
	sortedCopy := make([]string, len(names))
	copy(sortedCopy, names)
	add(&sumr, "entries emitted in sorted archive order",
		sort.StringsAreSorted(sortedCopy), "")

	// Check 8: round-trip extraction yields a readable archive.
	add(&sumr, "archive round-trips through archive/tar", roundTripOK(ba), "")

	// Check 9-12: symlink security.
	escapeRel := filepath.Join(base, "escape-rel")
	must(os.MkdirAll(escapeRel, 0o755))
	secretDir := filepath.Join(base, "secret")
	must(os.MkdirAll(secretDir, 0o755))
	writeFile(filepath.Join(secretDir, "secret.txt"), "topsecret")
	rel, _ := filepath.Rel(escapeRel, filepath.Join(secretDir, "secret.txt"))
	must(os.Symlink(filepath.Join("..", rel), filepath.Join(escapeRel, "evil")))
	add(&sumr, "relative symlink escaping root rejected", packFails(escapeRel), "")

	escapeAbs := filepath.Join(base, "escape-abs")
	must(os.MkdirAll(escapeAbs, 0o755))
	absSecretDir, err := filepath.Abs(secretDir)
	if err != nil {
		fatal(err)
	}
	must(os.Symlink(absSecretDir, filepath.Join(escapeAbs, "evil")))
	add(&sumr, "absolute symlink escaping root rejected", packFails(escapeAbs),
		"link target: "+absSecretDir)

	brokenEscape := filepath.Join(base, "escape-broken")
	must(os.MkdirAll(brokenEscape, 0o755))
	must(os.Symlink("../../does-not-exist-outside", filepath.Join(brokenEscape, "evil")))
	add(&sumr, "broken symlink escaping root rejected", packFails(brokenEscape), "")

	loop := filepath.Join(base, "loop")
	must(os.MkdirAll(loop, 0o755))
	must(os.Symlink("b", filepath.Join(loop, "a")))
	must(os.Symlink("a", filepath.Join(loop, "b")))
	add(&sumr, "symlink loop rejected", packFails(loop), "")

	// Check 13-16: build service end to end over HTTP.
	baseSvc := filepath.Join(base, "service")
	svc, err := builder.New(builder.Config{
		WorkDir:        filepath.Join(baseSvc, "work"),
		CacheDir:       filepath.Join(baseSvc, "cache"),
		DataDir:        filepath.Join(baseSvc, "data"),
		FixtureTimeout: 10 * time.Second,
	})
	if err != nil {
		fatal(err)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		fatal(err)
	}
	httpsrv := &http.Server{Handler: api.NewServer(svc, nil).Routes()}
	go httpsrv.Serve(ln)
	defer httpsrv.Shutdown(context.Background())
	baseURL := "http://" + ln.Addr().String()

	reqBody := map[string]any{
		"files": []map[string]string{
			{"path": "inputs/数据.txt", "content": "fixed\n"},
		},
		"fixtures": []map[string]any{
			{"name": "generate", "args": []string{"sh", "-c", "mkdir -p out && printf done > out/result.txt"}},
		},
	}
	rec1 := httpPostBuild(baseURL, reqBody)
	add(&sumr, "HTTP build with fixture succeeds",
		rec1.Status == "ok", fmt.Sprintf("id=%s status=%s err=%s", rec1.ID, rec1.Status, rec1.Error))
	rec2 := httpPostBuild(baseURL, reqBody)
	add(&sumr, "repeated HTTP build is cache-identical",
		rec2.Status == "ok" && rec1.Artifact != nil && rec2.Artifact != nil &&
			rec1.Artifact.Sha256 == rec2.Artifact.Sha256,
		fmt.Sprintf("sha1=%v sha2=%v", artSha(rec1), artSha(rec2)))

	badBody := map[string]any{
		"files": []map[string]string{{"path": "seed", "content": "x"}},
		"fixtures": []map[string]any{
			{"name": "evil", "args": []string{"sh", "-c", "ln -s ../../../../tmp escape"}},
		},
	}
	badRec := httpPostBuild(baseURL, badBody)
	add(&sumr, "HTTP rejects artifact when fixture creates escaping symlink",
		badRec.Status == "error" && badRec.Artifact == nil &&
			strings.Contains(badRec.Error, "escapes"),
		fmt.Sprintf("status=%s err=%s", badRec.Status, badRec.Error))

	artBytes := httpGetArtifact(baseURL, rec1.ID)
	add(&sumr, "HTTP artifact bytes match cached sha256",
		rec1.Artifact != nil && shaHex(artBytes) == rec1.Artifact.Sha256,
		fmt.Sprintf("header_sha=%v body_sha=%s", artSha(rec1), shaHex(artBytes)))

	for _, c := range sumr.Checks {
		if c.Pass {
			sumr.Passed++
			fmt.Printf("PASS  %s\n", c.Name)
		} else {
			sumr.Failed++
			fmt.Printf("FAIL  %s  [%s]\n", c.Name, c.Detail)
		}
	}
	fmt.Printf("\nsummary: %d passed, %d failed\n", sumr.Passed, sumr.Failed)
	writeJSON(*jsonOut, sumr)

	if sumr.Failed > 0 {
		os.Exit(1)
	}
}

// ---- fixture tree ----

func buildTree(root string) {
	must(os.MkdirAll(filepath.Join(root, "目录", "sub"), 0o755))
	must(os.MkdirAll(filepath.Join(root, "café", "naïve"), 0o755))
	must(os.MkdirAll(filepath.Join(root, "empty-dir"), 0o755))
	must(os.MkdirAll(filepath.Join(root, "emoji-😀"), 0o755))
	writeFile(filepath.Join(root, "文件.txt"), "unicode 内容\n")
	writeFile(filepath.Join(root, "café", "naïve", "名前.txt"), "こんにちは\n")
	writeFile(filepath.Join(root, "emoji-😀", "🎉.dat"), "party\n")
	writeFile(filepath.Join(root, "普通.bin"), strings.Repeat("payload-", 100))
	writeFile(filepath.Join(root, strings.Repeat("λ", 60)+".txt"), "long unicode name\n")
	must(os.Symlink("文件.txt", filepath.Join(root, "link-文件")))
}

func inspectTree(root string, mtime int64) treeInfo {
	var ti treeInfo
	ti.Path = root
	ti.MtimeEpoch = mtime
	_ = filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil || path == root {
			return err
		}
		switch {
		case info.Mode()&os.ModeSymlink != 0:
			ti.Symlinks++
		case info.IsDir():
			ti.Dirs++
		default:
			ti.Files++
		}
		return nil
	})
	return ti
}

// ---- archive helpers ----

func add(s *summary, name string, pass bool, detail string) {
	s.Checks = append(s.Checks, check{Name: name, Pass: pass, Detail: detail})
}

func pack(src, dst string, shuffleSeed int64) {
	f, err := os.Create(dst)
	must(err)
	opts := &archive.Options{}
	if shuffleSeed != 0 {
		opts.Lister = newSeededLister(shuffleSeed)
	}
	_, _, err = archive.WriteTarHashed(f, src, opts)
	must(err)
	must(f.Close())
}

func packFails(src string) bool {
	var buf bytes.Buffer
	_, err := archive.WriteTar(&buf, src, nil)
	return err != nil
}

func headers(b []byte) ([]string, []time.Time) {
	tr := tar.NewReader(bytes.NewReader(b))
	var names []string
	var mtimes []time.Time
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return names, mtimes
		}
		must(err)
		names = append(names, hdr.Name)
		mtimes = append(mtimes, hdr.ModTime)
	}
}

func roundTripOK(b []byte) bool {
	tr := tar.NewReader(bytes.NewReader(b))
	n := 0
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return n > 0
		}
		if err != nil {
			return false
		}
		if hdr.Typeflag == tar.TypeReg {
			if _, err := io.Copy(io.Discard, tr); err != nil {
				return false
			}
		}
		n++
	}
}

func contains(names []string, want string) bool {
	for _, n := range names {
		if n == want {
			return true
		}
	}
	return false
}

func containsAll(names []string, wants ...string) bool {
	for _, w := range wants {
		if !contains(names, w) {
			return false
		}
	}
	return true
}

func clobberTimes(root string, when time.Time) {
	must(filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		return os.Chtimes(path, when, when)
	}))
}

// ---- misc ----

func artSha(r *builder.Record) string {
	if r.Artifact == nil {
		return "<none>"
	}
	return r.Artifact.Sha256
}

func writeJSON(path string, v any) {
	b, err := json.MarshalIndent(v, "", "  ")
	must(err)
	must(os.WriteFile(path, b, 0o644))
}

func readFile(p string) []byte {
	b, err := os.ReadFile(p)
	must(err)
	return b
}

func writeFile(p, content string) {
	must(os.MkdirAll(filepath.Dir(p), 0o755))
	must(os.WriteFile(p, []byte(content), 0o644))
}

func shaHex(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func must(err error) {
	if err != nil {
		fatal(err)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "acceptance fatal:", err)
	os.Exit(2)
}
