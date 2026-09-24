package cachekey

import (
	"os"
	"path/filepath"
	"testing"
)

func writeFile(t *testing.T, root, name, content string) {
	t.Helper()
	p := filepath.Join(root, name)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func baseInputs() Inputs {
	return Inputs{
		Platform: "linux/amd64",
		Command:  "gcc -o $OUT main.c",
		Env:      map[string]string{"CPATH": "inc"},
		Toolchain: Toolchain{
			Name: "gcc", ResolvedPath: "/usr/bin/gcc",
			BinarySHA256: "aa", VersionSHA256: "bb",
		},
		Sources: []SourceEntry{
			{Path: "a.c", State: StatePresent, Size: 3, SHA256: "aaa"},
			{Path: "b.c", State: StatePresent, Size: 0, SHA256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
		},
	}
}

func TestManifestDeterministicOrder(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "z.c", "z")
	writeFile(t, root, "a.c", "a")
	writeFile(t, root, "sub/m.c", "m")

	m1, err := BuildManifest(root, []string{"z.c", "a.c", "sub/m.c"})
	if err != nil {
		t.Fatal(err)
	}
	m2, err := BuildManifest(root, []string{"sub/m.c", "z.c", "a.c"})
	if err != nil {
		t.Fatal(err)
	}
	in1, in2 := baseInputs(), baseInputs()
	in1.Sources, in2.Sources = m1, m2
	if in1.Key() != in2.Key() {
		t.Fatal("declaration order must not affect the key")
	}
	got := []string{m1[0].Path, m1[1].Path, m1[2].Path}
	want := []string{"a.c", "sub/m.c", "z.c"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("manifest not byte-sorted: got %v want %v", got, want)
		}
	}
}

func TestMissingFileDistinctFromEmptyFile(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "empty.c", "") // exists, zero bytes

	m, err := BuildManifest(root, []string{"empty.c", "gone.c"})
	if err != nil {
		t.Fatal(err)
	}
	if m[0].State != StatePresent || m[0].Size != 0 {
		t.Fatalf("empty.c: got %+v, want present size 0", m[0])
	}
	if m[0].SHA256 != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" {
		t.Fatalf("empty file must hash to the empty SHA-256, got %s", m[0].SHA256)
	}
	if m[1].State != StateMissing || m[1].SHA256 != "" {
		t.Fatalf("gone.c: got %+v, want missing with no hash", m[1])
	}

	withEmpty := baseInputs()
	withEmpty.Sources = m[:1]
	withMissing := baseInputs()
	withMissing.Sources = m[1:]
	if withEmpty.Key() == withMissing.Key() {
		t.Fatal("a missing file and an empty file must produce different keys")
	}
}

func TestKeyBindsAllInputs(t *testing.T) {
	base := baseInputs().Key()

	mut := func(f func(*Inputs)) string {
		in := baseInputs()
		f(&in)
		return in.Key()
	}
	cases := map[string]string{
		"env value":    mut(func(i *Inputs) { i.Env["CPATH"] = "other" }),
		"env omitted":  mut(func(i *Inputs) { i.Env = nil }),
		"env added":    mut(func(i *Inputs) { i.Env["X"] = "1" }),
		"command":      mut(func(i *Inputs) { i.Command = "gcc -O3 -o $OUT main.c" }),
		"platform":     mut(func(i *Inputs) { i.Platform = "linux/arm64" }),
		"tool binary":  mut(func(i *Inputs) { i.Toolchain.BinarySHA256 = "cc" }),
		"tool version": mut(func(i *Inputs) { i.Toolchain.VersionSHA256 = "dd" }),
		"source bytes": mut(func(i *Inputs) { i.Sources[0].SHA256 = "fff" }),
		"source set":   mut(func(i *Inputs) { i.Sources = i.Sources[:1] }),
	}
	for name, key := range cases {
		if key == base {
			t.Errorf("changing %s did not change the cache key", name)
		}
	}
}

func TestDuplicateSourcesDoNotChangeKey(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "a.c", "a")
	m1, _ := BuildManifest(root, []string{"a.c"})
	m2, _ := BuildManifest(root, []string{"a.c", "a.c"})
	in1, in2 := baseInputs(), baseInputs()
	in1.Sources, in2.Sources = m1, m2
	if in1.Key() != in2.Key() {
		t.Fatal("duplicate declaration must not change the key")
	}
}

func TestPathEscapeRejected(t *testing.T) {
	root := t.TempDir()
	for _, bad := range []string{"../x.c", "/etc/passwd", "sub/../../x.c"} {
		if _, err := BuildManifest(root, []string{bad}); err == nil {
			t.Errorf("path %q should be rejected", bad)
		}
	}
}

func TestContentChangeChangesKey(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "a.c", "one")
	m1, _ := BuildManifest(root, []string{"a.c"})
	writeFile(t, root, "a.c", "two")
	m2, _ := BuildManifest(root, []string{"a.c"})
	in1, in2 := baseInputs(), baseInputs()
	in1.Sources, in2.Sources = m1, m2
	if in1.Key() == in2.Key() {
		t.Fatal("editing a source must invalidate the key")
	}
}
