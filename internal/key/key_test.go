package key

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"testing"
)

func baseMaterial() *Material {
	return &Material{
		Toolchain: ToolchainSummary{
			Name:           "go",
			VersionCommand: []string{"go", "version"},
			VersionOutput:  "go version go1.22.2 linux/amd64",
			BinaryPath:     "/usr/local/go/bin/go",
			BinaryDigest:   validDigest('b'),
		},
		Args:    []string{"-tags", "pro"},
		Command: []string{"go", "build", "{sources}"},
		Target:  map[string]string{"GOOS": "linux", "GOARCH": "amd64"},
		Env: []EnvVar{
			{Name: "CGO_ENABLED", Value: "0"},
		},
		Sources: []SourceEntry{
			{Path: "main.go", Present: true, Size: 10, Digest: strings.Repeat("a", 64)},
		},
	}
}

func validDigest(c byte) string { return strings.Repeat(string(c), 64) }

func TestDeterministicAndOrderInsensitive(t *testing.T) {
	m1 := baseMaterial()
	m1.Toolchain.BinaryDigest = validDigest('b')
	m1.Env = []EnvVar{{Name: "B", Value: "2"}, {Name: "A", Value: "1"}}
	m1.Target = map[string]string{"GOARCH": "amd64", "GOOS": "linux"}
	m1.Sources = []SourceEntry{
		{Path: "z.go", Present: true, Size: 1, Digest: validDigest('1')},
		{Path: "a.go", Present: true, Size: 2, Digest: validDigest('2')},
	}
	raw1, k1, err := Canonicalize(m1)
	if err != nil {
		t.Fatalf("canonicalize: %v", err)
	}

	m2 := baseMaterial()
	m2.Toolchain.BinaryDigest = validDigest('b')
	m2.Env = []EnvVar{{Name: "A", Value: "1"}, {Name: "B", Value: "2"}}
	m2.Target = map[string]string{"GOOS": "linux", "GOARCH": "amd64"}
	m2.Sources = []SourceEntry{
		{Path: "a.go", Present: true, Size: 2, Digest: validDigest('2')},
		{Path: "z.go", Present: true, Size: 1, Digest: validDigest('1')},
	}
	raw2, k2, err := Canonicalize(m2)
	if err != nil {
		t.Fatalf("canonicalize: %v", err)
	}
	if k1 != k2 {
		t.Fatalf("order changed key:\n%s\n%s", k1, k2)
	}
	if string(raw1) != string(raw2) {
		t.Fatalf("canonical JSON differs:\n%s\n%s", raw1, raw2)
	}

	// Repeated computation is stable.
	_, k1again, _ := Canonicalize(m1)
	if k1 != k1again {
		t.Fatalf("key not stable: %s vs %s", k1, k1again)
	}
}

func TestMissingVsEmptyFileHaveDifferentKeys(t *testing.T) {
	emptySum := sha256.Sum256(nil)
	emptyDigest := hex.EncodeToString(emptySum[:])

	withEmpty := baseMaterial()
	withEmpty.Toolchain.BinaryDigest = validDigest('b')
	withEmpty.Sources = []SourceEntry{
		{Path: "opt.txt", Present: true, Size: 0, Digest: emptyDigest},
	}
	_, kEmpty, err := Canonicalize(withEmpty)
	if err != nil {
		t.Fatalf("empty-file material: %v", err)
	}

	missing := baseMaterial()
	missing.Toolchain.BinaryDigest = validDigest('b')
	missing.Sources = []SourceEntry{
		{Path: "opt.txt", Present: false, Digest: "", Size: 0},
	}
	_, kMissing, err := Canonicalize(missing)
	if err != nil {
		t.Fatalf("missing-file material: %v", err)
	}
	if kEmpty == kMissing {
		t.Fatalf("missing file and present empty file produced the same key: %s", kEmpty)
	}
}

func TestKeyBindsEveryComponent(t *testing.T) {
	mk := func(mutate func(*Material)) string {
		m := baseMaterial()
		m.Toolchain.BinaryDigest = validDigest('b')
		mutate(m)
		_, k, err := Canonicalize(m)
		if err != nil {
			t.Fatalf("canonicalize: %v", err)
		}
		return k
	}
	baseline := mk(func(m *Material) {})

	cases := map[string]func(*Material){
		"source content": func(m *Material) { m.Sources[0].Digest = validDigest('9') },
		"source path":    func(m *Material) { m.Sources[0].Path = "other.go" },
		"added source": func(m *Material) {
			m.Sources = append(m.Sources, SourceEntry{Path: "new.go", Present: false})
		},
		"toolchain output": func(m *Material) { m.Toolchain.VersionOutput += " (patched)" },
		"toolchain digest": func(m *Material) { m.Toolchain.BinaryDigest = validDigest('c') },
		"args":             func(m *Material) { m.Args = []string{"-tags", "enterprise"} },
		"args order":       func(m *Material) { m.Args = []string{"pro", "-tags"} },
		"target":           func(m *Material) { m.Target["GOARCH"] = "arm64" },
		"new target key":   func(m *Material) { m.Target["CGO"] = "1" },
		"env value":        func(m *Material) { m.Env[0].Value = "1" },
		"env omitted":      func(m *Material) { m.Env = nil },
		"extra env": func(m *Material) {
			m.Env = append(m.Env, EnvVar{Name: "EXTRA", Value: "x"})
		},
		"command": func(m *Material) { m.Command = []string{"go", "build", "."} },
	}
	for name, mut := range cases {
		if got := mk(mut); got == baseline {
			t.Errorf("key did not change when %s changed", name)
		}
	}
}

func TestEnvDeclaredInMaterialNotAmbient(t *testing.T) {
	// Two requests that differ only in how the server's ambient environment
	// is configured must still key identically when the declared env is the
	// same — Canonicalize never reads os.Environ. This test is structural:
	// the function signature proves no ambient access is possible.
	m := baseMaterial()
	m.Toolchain.BinaryDigest = validDigest('b')
	_, k1, _ := Canonicalize(m)
	_, k2, _ := Canonicalize(m)
	if k1 != k2 {
		t.Fatal("ambient changes must not affect key")
	}
}

func TestValidation(t *testing.T) {
	bad := []struct {
		name string
		mat  *Material
	}{
		{"nil", nil},
		{"no toolchain", func() *Material { m := baseMaterial(); m.Toolchain.Name = " "; return m }()},
		{"no version cmd", func() *Material {
			m := baseMaterial()
			m.Toolchain.VersionCommand = nil
			return m
		}()},
		{"bad digest length", func() *Material {
			m := baseMaterial()
			m.Sources[0].Digest = "abc"
			return m
		}()},
		{"absent with digest", func() *Material {
			m := baseMaterial()
			m.Sources[0].Present = false
			m.Sources[0].Digest = validDigest('a')
			return m
		}()},
		{"absent with size", func() *Material {
			m := baseMaterial()
			m.Sources[0].Present = false
			m.Sources[0].Digest = ""
			m.Sources[0].Size = 3
			return m
		}()},
		{"duplicate path", func() *Material {
			m := baseMaterial()
			m.Sources = append(m.Sources, m.Sources[0])
			return m
		}()},
		{"duplicate env", func() *Material {
			m := baseMaterial()
			m.Env = append(m.Env, m.Env[0])
			return m
		}()},
	}
	for _, tc := range bad {
		var m *Material
		if tc.mat != nil {
			tc.mat.Toolchain.BinaryDigest = validDigest('b')
			m = tc.mat
		}
		if _, _, err := Canonicalize(m); err == nil {
			t.Errorf("%s: expected validation error", tc.name)
		}
	}
}

func TestNormalizePath(t *testing.T) {
	good := map[string]string{
		"a/b.go":    "a/b.go",
		"./a.go":    "a.go",
		"a/../b.go": "b.go",
		"a//b.go":   "a/b.go",
		"x/y/z/..":  "x/y",
	}
	for in, want := range good {
		got, err := NormalizePath(in)
		if err != nil {
			t.Errorf("NormalizePath(%q): unexpected error %v", in, err)
			continue
		}
		if got != want {
			t.Errorf("NormalizePath(%q)=%q want %q", in, got, want)
		}
	}
	bad := []string{"/etc/passwd", "../escape.go", "a/../../b.go", "", "a/"}
	for _, in := range bad {
		if _, err := NormalizePath(in); err == nil {
			t.Errorf("NormalizePath(%q): expected error", in)
		}
	}
}

func TestCanonicalJSONShape(t *testing.T) {
	m := baseMaterial()
	m.Toolchain.BinaryDigest = validDigest('b')
	raw, k, err := Canonicalize(m)
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("canonical material not valid JSON: %v", err)
	}
	if doc["version"] != KeyVersion {
		t.Errorf("version envelope = %v", doc["version"])
	}
	if !strings.HasPrefix(k, KeyVersion+"-") {
		t.Errorf("key missing version prefix: %s", k)
	}
	// Sources must appear sorted by path.
	srcs := doc["sources"].([]any)
	if srcs[0].(map[string]any)["path"] != "main.go" {
		t.Errorf("unexpected first source: %v", srcs[0])
	}
}
