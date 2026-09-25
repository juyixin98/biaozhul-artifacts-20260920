package archive

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"math/rand"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"
)

// buildTree populates root with the acceptance fixture content:
// Unicode paths, nested dirs, an empty directory, a long file name,
// a file with non-0644 perms and an in-tree symlink.
func buildTree(t *testing.T, root string) {
	t.Helper()
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	must(os.MkdirAll(filepath.Join(root, "目录", "sub"), 0o755))
	must(os.MkdirAll(filepath.Join(root, "café", "naïve"), 0o755))
	must(os.MkdirAll(filepath.Join(root, "empty-dir"), 0o755))
	must(os.MkdirAll(filepath.Join(root, "emoji-😀"), 0o755))
	must(os.WriteFile(filepath.Join(root, "文件.txt"), []byte("unicode 内容\n"), 0o640))
	must(os.WriteFile(filepath.Join(root, "café", "naïve", "名前.txt"), []byte("こんにちは\n"), 0o644))
	must(os.WriteFile(filepath.Join(root, "emoji-😀", "🎉.dat"), []byte("party\n"), 0o600))
	must(os.WriteFile(filepath.Join(root, "script.sh"), []byte("#!/bin/sh\necho ok\n"), 0o755))
	// File name > 100 bytes to force PAX extended header.
	longName := strings.Repeat("a", 140) + ".txt"
	must(os.WriteFile(filepath.Join(root, longName), []byte("long name payload\n"), 0o644))
	// In-tree symlinks: same directory and into a subdirectory.
	must(os.Symlink("文件.txt", filepath.Join(root, "link-to-file")))
	must(os.Symlink(filepath.Join("café", "naïve"), filepath.Join(root, "link-to-dir")))
}

func tarBytes(t *testing.T, root string, opts *Options) []byte {
	t.Helper()
	var buf bytes.Buffer
	if _, err := WriteTar(&buf, root, opts); err != nil {
		t.Fatalf("WriteTar(%s): %v", root, err)
	}
	return buf.Bytes()
}

func hashHex(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

// Core property: identical content, differing mtimes -> identical bytes.
func TestDeterministicAcrossMtimes(t *testing.T) {
	a := t.TempDir()
	b := t.TempDir()
	buildTree(t, a)
	buildTree(t, b)

	old := time.Unix(1_000_000_000, 0)
	newer := time.Unix(2_000_000_000, 123_456_789)
	// Give every entry in A old times and B new times.
	clobberTimes(t, a, old)
	clobberTimes(t, b, newer)

	ta := tarBytes(t, a, nil)
	tb := tarBytes(t, b, nil)
	if !bytes.Equal(ta, tb) {
		t.Fatalf("archives differ despite identical content:\n%s\n%s", hashHex(ta), hashHex(tb))
	}

	// Headers must carry the fixed epoch time, never the source mtimes.
	names, mtimes := readHeaders(t, bytes.NewReader(ta))
	want := []string{
		strings.Repeat("a", 140) + ".txt",
		"café/", "café/naïve/", "café/naïve/名前.txt",
		"emoji-😀/", "emoji-😀/🎉.dat",
		"empty-dir/",
		"link-to-dir", "link-to-file",
		"script.sh",
		"文件.txt",
		"目录/", "目录/sub/",
	}
	if strings.Join(names, "\n") != strings.Join(want, "\n") {
		t.Fatalf("entry order/set mismatch:\n%v", names)
	}
	for _, mt := range mtimes {
		if !mt.Equal(time.Unix(0, 0).UTC()) {
			t.Fatalf("entry mtime %v is not the fixed epoch", mt)
		}
	}
}

// The same tree archived through a deliberately shuffled directory lister
// must produce byte-identical output to the normal sorted lister.
func TestInvariantToReaddirOrder(t *testing.T) {
	root := t.TempDir()
	buildTree(t, root)
	baseline := tarBytes(t, root, nil)

	rng := rand.New(rand.NewSource(42))
	shuffled := tarBytes(t, root, &Options{Lister: &shuffleLister{rng: rng}})
	if !bytes.Equal(baseline, shuffled) {
		t.Fatalf("shuffled readdir changed archive:\n%s\n%s",
			hashHex(baseline), hashHex(shuffled))
	}
}

func TestModesAndIdentitiesAreNormalized(t *testing.T) {
	root := t.TempDir()
	buildTree(t, root)
	tr := tar.NewReader(bytes.NewReader(tarBytes(t, root, nil)))
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		if hdr.Uid != 0 || hdr.Gid != 0 || hdr.Uname != "" || hdr.Gname != "" {
			t.Fatalf("%s: uid/gid/uname/gname not normalized: %+v", hdr.Name, hdr)
		}
		if !hdr.AccessTime.IsZero() || !hdr.ChangeTime.IsZero() {
			t.Fatalf("%s: atime/ctime must be zero, got %v %v", hdr.Name, hdr.AccessTime, hdr.ChangeTime)
		}
		switch hdr.Typeflag {
		case tar.TypeDir:
			if hdr.Mode != 0o755 {
				t.Fatalf("dir %s mode %o", hdr.Name, hdr.Mode)
			}
		case tar.TypeReg:
			want := int64(0o644)
			if hdr.Name == "script.sh" {
				want = 0o644 // default policy flattens executable bit
			}
			if hdr.Mode != want {
				t.Fatalf("file %s mode %o, want %o", hdr.Name, hdr.Mode, want)
			}
		case tar.TypeSymlink:
			if hdr.Mode != 0o777 {
				t.Fatalf("symlink %s mode %o", hdr.Name, hdr.Mode)
			}
		}
	}
}

func TestPreserveSourceModesOptIn(t *testing.T) {
	root := t.TempDir()
	buildTree(t, root)
	opts := &Options{FileMode: PreserveMode, DirMode: PreserveMode}
	tr := tar.NewReader(bytes.NewReader(tarBytes(t, root, opts)))
	found := false
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		if hdr.Name == "script.sh" {
			found = true
			if hdr.Mode != 0o755 {
				t.Fatalf("script.sh mode %o, want preserved 0755", hdr.Mode)
			}
		}
	}
	if !found {
		t.Fatal("script.sh missing")
	}
}

func TestEmptyTreeProducesValidEmptyTar(t *testing.T) {
	root := t.TempDir()
	b := tarBytes(t, root, nil)
	if len(b) != 1024 { // two zero blocks
		t.Fatalf("empty tree tar size = %d, want 1024", len(b))
	}
	if !bytes.Equal(b, make([]byte, 1024)) {
		t.Fatal("empty tree tar not zero blocks")
	}
}

func TestEmptyDirectoryIsStored(t *testing.T) {
	root := t.TempDir()
	buildTree(t, root)
	names, _ := readHeaders(t, bytes.NewReader(tarBytes(t, root, nil)))
	var sawEmpty bool
	for _, n := range names {
		if n == "empty-dir/" {
			sawEmpty = true
		}
	}
	if !sawEmpty {
		t.Fatal("empty directory missing from archive")
	}
}

func TestContentDifferenceChangesHash(t *testing.T) {
	a := t.TempDir()
	b := t.TempDir()
	buildTree(t, a)
	buildTree(t, b)
	if err := os.WriteFile(filepath.Join(b, "文件.txt"), []byte("different\n"), 0o640); err != nil {
		t.Fatal(err)
	}
	ha := hashHex(tarBytes(t, a, nil))
	hb := hashHex(tarBytes(t, b, nil))
	if ha == hb {
		t.Fatal("different content produced identical archives")
	}
}

func TestCustomFixedTime(t *testing.T) {
	root := t.TempDir()
	must(t, os.WriteFile(filepath.Join(root, "f"), []byte("x"), 0o644))
	when := time.Unix(946684800, 0).UTC() // 2000-01-01
	tr := tar.NewReader(bytes.NewReader(tarBytes(t, root, &Options{FixedTime: when})))
	hdr, err := tr.Next()
	if err != nil {
		t.Fatal(err)
	}
	if !hdr.ModTime.Equal(when) {
		t.Fatalf("mtime = %v, want %v", hdr.ModTime, when)
	}
}

// ---- symlink security ----

func TestSymlinkEscapeRelative(t *testing.T) {
	root := t.TempDir()
	must(t, os.WriteFile(filepath.Join(root, "inside"), []byte("x"), 0o644))
	outside := filepath.Join(t.TempDir(), "secret")
	must(t, os.WriteFile(outside, []byte("secret"), 0o644))
	// root/evil -> ../../../etc-ish target landing outside root
	rel, err := filepath.Rel(root, outside)
	if err != nil {
		t.Fatal(err)
	}
	evilTarget := filepath.Join("..", rel) // equivalent: outside
	must(t, os.Symlink(evilTarget, filepath.Join(root, "evil")))
	var buf bytes.Buffer
	if _, err := WriteTar(&buf, root, nil); err == nil {
		t.Fatal("expected escape symlink to be rejected")
	} else {
		var ese *ErrSymlinkEscape
		if !asErr(err, &ese) {
			t.Fatalf("want ErrSymlinkEscape, got %v", err)
		}
	}
}

func TestSymlinkEscapeAbsolute(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	must(t, os.Symlink(outside, filepath.Join(root, "abslink")))
	var buf bytes.Buffer
	_, err := WriteTar(&buf, root, nil)
	if err == nil {
		t.Fatal("expected absolute escape symlink to be rejected")
	}
}

func TestSymlinkLoopRejected(t *testing.T) {
	root := t.TempDir()
	must(t, os.Symlink("b", filepath.Join(root, "a")))
	must(t, os.Symlink("a", filepath.Join(root, "b")))
	var buf bytes.Buffer
	_, err := WriteTar(&buf, root, nil)
	if err == nil {
		t.Fatal("expected symlink loop to be rejected")
	}
}

func TestBrokenSymlinkInsideRootAllowed(t *testing.T) {
	root := t.TempDir()
	must(t, os.Symlink("does-not-exist", filepath.Join(root, "broken")))
	b := tarBytes(t, root, nil)
	tr := tar.NewReader(bytes.NewReader(b))
	hdr, err := tr.Next()
	if err != nil {
		t.Fatal(err)
	}
	if hdr.Typeflag != tar.TypeSymlink || hdr.Linkname != "does-not-exist" {
		t.Fatalf("broken in-tree link not preserved: %+v", hdr)
	}
}

func TestBrokenSymlinkEscapingRootRejected(t *testing.T) {
	root := t.TempDir()
	must(t, os.Symlink("../../nonexistent-target", filepath.Join(root, "evil")))
	var buf bytes.Buffer
	if _, err := WriteTar(&buf, root, nil); err == nil {
		t.Fatal("expected broken escaping symlink to be rejected")
	}
}

func TestFIFORejected(t *testing.T) {
	root := t.TempDir()
	fifo := filepath.Join(root, "pipe")
	if err := mkFifo(fifo); err != nil {
		t.Skipf("cannot create fifo: %v", err)
	}
	var buf bytes.Buffer
	if _, err := WriteTar(&buf, root, nil); err == nil {
		t.Fatal("expected fifo to be rejected")
	}
}

func TestSourceRootNotADirectory(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "file")
	must(t, os.WriteFile(f, []byte("x"), 0o644))
	var buf bytes.Buffer
	if _, err := WriteTar(&buf, f, nil); err == nil {
		t.Fatal("expected error for file root")
	}
}

func TestValidateTreeRejectsEscape(t *testing.T) {
	root := t.TempDir()
	must(t, os.Symlink("..", filepath.Join(root, "up")))
	if err := ValidateTree(root); err == nil {
		t.Fatal("ValidateTree accepted escaping symlink")
	}
}

// ---- helpers ----

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func clobberTimes(t *testing.T, root string, when time.Time) {
	t.Helper()
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		return os.Chtimes(path, when, when)
	})
	if err != nil {
		t.Fatal(err)
	}
}

func readHeaders(t *testing.T, r io.Reader) (names []string, mtimes []time.Time) {
	t.Helper()
	tr := tar.NewReader(r)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return names, mtimes
		}
		if err != nil {
			t.Fatal(err)
		}
		names = append(names, hdr.Name)
		mtimes = append(mtimes, hdr.ModTime)
	}
}

func asErr(err error, target **ErrSymlinkEscape) bool {
	for err != nil {
		if e, ok := err.(*ErrSymlinkEscape); ok {
			*target = e
			return true
		}
		type unwrapper interface{ Unwrap() error }
		u, ok := err.(unwrapper)
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}

// shuffleLister returns directory entries in randomized order while
// delegating everything else to the os-backed lister.
type shuffleLister struct{ rng *rand.Rand }

func (s *shuffleLister) ReadDir(name string) ([]os.DirEntry, error) {
	des, err := os.ReadDir(name)
	if err != nil {
		return nil, err
	}
	out := make([]os.DirEntry, len(des))
	copy(out, des)
	s.rng.Shuffle(len(out), func(i, j int) { out[i], out[j] = out[j], out[i] })
	return out, nil
}
func (s *shuffleLister) Lstat(name string) (os.FileInfo, error) {
	return os.Lstat(name)
}

// keep sort imported even if helper set changes
var _ = sort.Strings
