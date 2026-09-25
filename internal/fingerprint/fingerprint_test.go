package fingerprint

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"cdag/internal/graph"
)

func TestKeyIgnoresTimestamps(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "f.txt")
	if err := os.WriteFile(f, []byte("same bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
	keyAt := func() string {
		refs, err := HashFiles(dir, []string{"f.txt"})
		if err != nil {
			t.Fatal(err)
		}
		fp := Input(&graph.Node{ID: "n"}, refs, nil, nil, nil)
		k, err := fp.Key()
		if err != nil {
			t.Fatal(err)
		}
		return k
	}
	k1 := keyAt()
	future := time.Now().Add(5 * time.Hour)
	if err := os.Chtimes(f, future, future); err != nil {
		t.Fatal(err)
	}
	k2 := keyAt()
	if k1 != k2 {
		t.Fatalf("fingerprint must ignore mtime: %s != %s", k1, k2)
	}

	// Content change must change the key.
	if err := os.WriteFile(f, []byte("different bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
	k3 := keyAt()
	if k3 == k1 {
		t.Fatal("content change must change the key")
	}
}

func TestKeyCoversParamsEnvToolsDeps(t *testing.T) {
	base := func() Fingerprint {
		return Input(
			&graph.Node{
				ID:     "n",
				Params: map[string]string{"p": "1"},
				Env:    []string{"HOME"},
			},
			nil,
			[]DepRef{{ID: "d", Fingerprint: "depkey"}},
			[]ToolVersion{{Name: "tr", Version: "v1"}},
			map[string]string{"HOME": "/home/a"},
		)
	}
	k1, _ := base().Key()

	// Params.
	fp := base()
	fp.Params[0].Val = "2"
	if k2, _ := fp.Key(); k2 == k1 {
		t.Fatal("param change must change key")
	}

	// Env value.
	fp = base()
	fp.Env[0].Val = "/home/b"
	if k2, _ := fp.Key(); k2 == k1 {
		t.Fatal("env change must change key")
	}

	// Tool version.
	fp = base()
	fp.Tools[0].Version = "v2"
	if k2, _ := fp.Key(); k2 == k1 {
		t.Fatal("tool version change must change key")
	}

	// Dependency fingerprint.
	fp = base()
	fp.Deps[0].Fingerprint = "depkey2"
	if k2, _ := fp.Key(); k2 == k1 {
		t.Fatal("dependency fingerprint change must change key")
	}
}

func TestOrderIndependent(t *testing.T) {
	n := &graph.Node{ID: "n", Params: map[string]string{"b": "2", "a": "1"}}
	fp1 := Input(n, nil, nil, []ToolVersion{{Name: "t", Version: "v"}}, nil)
	n2 := &graph.Node{ID: "n", Params: map[string]string{"a": "1", "b": "2"}}
	fp2 := Input(n2, nil, nil, []ToolVersion{{Name: "t", Version: "v"}}, nil)
	k1, _ := fp1.Key()
	k2, _ := fp2.Key()
	if k1 != k2 {
		t.Fatalf("map/argument ordering must not affect key: %s != %s", k1, k2)
	}
}
