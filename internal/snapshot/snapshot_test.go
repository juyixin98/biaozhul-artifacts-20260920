package snapshot

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/example/snapshotprune/internal/crypto"
	"github.com/example/snapshotprune/internal/state"
	"github.com/example/snapshotprune/internal/types"
)

func testManager(t *testing.T) (*Manager, string) {
	t.Helper()
	dir := t.TempDir()
	key, err := crypto.LoadOrCreateNodeKey(dir)
	if err != nil {
		t.Fatal(err)
	}
	m, err := NewManager(dir, "c1", key)
	if err != nil {
		t.Fatal(err)
	}
	return m, dir
}

func sampleTable() state.Table {
	a := types.Address{1}
	b := types.Address{2}
	return state.Table{
		a: {Nonce: 1, Balance: 100},
		b: {Nonce: 2, Balance: 200},
	}
}

func TestEncodeDecodeState(t *testing.T) {
	tbl := sampleTable()
	b, err := encodeState(tbl)
	if err != nil {
		t.Fatal(err)
	}
	out, err := decodeState(b)
	if err != nil {
		t.Fatal(err)
	}
	if state.Root(out) != state.Root(tbl) {
		t.Fatal("root changed across encode/decode")
	}
	// Corrupting a byte breaks the trailing checksum.
	b[20] ^= 1
	if _, err := decodeState(b); err == nil {
		t.Fatal("checksum did not detect corruption")
	}
}

func TestCommitRescanVerify(t *testing.T) {
	m, _ := testManager(t)
	tbl := sampleTable()
	id, err := m.BeginBuild(5)
	if err != nil {
		t.Fatal(err)
	}
	if err := m.Commit(5, id, tbl, nil); err != nil {
		t.Fatal(err)
	}
	if !m.Has(5) {
		t.Fatal("snapshot not listed after commit")
	}
	loaded, err := m.Load(5)
	if err != nil || state.Root(loaded) != state.Root(tbl) {
		t.Fatalf("load: %v", err)
	}

	// A fresh manager re-verifies on scan and still lists it.
	m2, _ := testManagerAt(t, m.Dir())
	if len(m2.List()) != 1 {
		t.Fatal("snapshot did not survive rescan/verification")
	}
}

func TestAcquireBlocksDelete(t *testing.T) {
	m, _ := testManager(t)
	id, _ := m.BeginBuild(1)
	if err := m.Commit(1, id, sampleTable(), nil); err != nil {
		t.Fatal(err)
	}
	release, err := m.Acquire(1)
	if err != nil {
		t.Fatal(err)
	}
	if err := m.Delete(1); err == nil {
		t.Fatal("delete with active ref succeeded")
	}
	release()
	if err := m.Delete(1); err != nil {
		t.Fatalf("delete after release: %v", err)
	}
}

func TestMissingStateFileIsQuarantined(t *testing.T) {
	m, _ := testManager(t)
	id, _ := m.BeginBuild(2)
	if err := m.Commit(2, id, sampleTable(), nil); err != nil {
		t.Fatal(err)
	}
	// Delete the state file from the only snap dir.
	entries, _ := os.ReadDir(m.Dir())
	for _, e := range entries {
		if len(e.Name()) > 4 && e.Name()[:5] == "snap-" {
			_ = os.Remove(filepath.Join(m.Dir(), e.Name(), "state.dat"))
		}
	}
	m2, _ := testManagerAt(t, m.Dir())
	if len(m2.List()) != 0 {
		t.Fatal("snapshot with missing file was listed")
	}
}

func TestPruneKeepsThreeAndProtectsPinned(t *testing.T) {
	m, _ := testManager(t)
	for _, h := range []uint64{2, 4, 6, 8, 10} {
		id, err := m.BeginBuild(h)
		if err != nil {
			t.Fatal(err)
		}
		if err := m.Commit(h, id, sampleTable(), nil); err != nil {
			t.Fatal(err)
		}
	}
	// Pin the oldest so retention cannot remove it.
	rel, err := m.Acquire(2)
	if err != nil {
		t.Fatal(err)
	}
	res, err := m.PruneOld()
	if err != nil {
		t.Fatal(err)
	}
	if len(res.DeletedHeights) != 1 || res.DeletedHeights[0] != 4 {
		t.Fatalf("deleted=%v, want only 4", res.DeletedHeights)
	}
	protected := false
	for _, h := range res.ProtectedHeights {
		if h == 2 {
			protected = true
		}
	}
	if !protected {
		t.Fatal("pinned oldest snapshot not protected")
	}
	rel()
	res2, err := m.PruneOld()
	if err != nil {
		t.Fatal(err)
	}
	if len(res2.DeletedHeights) != 1 || res2.DeletedHeights[0] != 2 {
		t.Fatalf("post-release deleted=%v, want 2", res2.DeletedHeights)
	}
	if len(m.List()) != KeepCount {
		t.Fatalf("kept %d, want %d", len(m.List()), KeepCount)
	}
}
