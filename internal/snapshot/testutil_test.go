package snapshot

import (
	"path/filepath"
	"testing"

	"github.com/example/snapshotprune/internal/crypto"
)

// testManagerAt opens a fresh manager over an existing data dir's snapshots
// parent (where the node key lives).
func testManagerAt(t *testing.T, snapParent string) (*Manager, string) {
	t.Helper()
	key, err := crypto.LoadOrCreateNodeKey(filepath.Dir(snapParent))
	if err != nil {
		t.Fatal(err)
	}
	m, err := NewManager(filepath.Dir(snapParent), "c1", key)
	if err != nil {
		t.Fatal(err)
	}
	return m, snapParent
}
