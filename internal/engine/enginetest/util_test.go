package enginetest

import (
	"crypto/ed25519"
	"path/filepath"
	"testing"

	"github.com/example/snapshotprune/internal/crypto"
	"github.com/example/snapshotprune/internal/snapshot"
	"github.com/example/snapshotprune/internal/types"
)

// reopenManager creates a fresh snapshot.Manager over the same data dir,
// simulating the scan a process performs after a crash/restart.
func reopenManager(t *testing.T, e *testEnv) *snapshot.Manager {
	t.Helper()
	key, err := crypto.LoadOrCreateNodeKey(filepath.Join(e.dir, "data"))
	if err != nil {
		t.Fatal(err)
	}
	mgr, err := snapshot.NewManager(filepath.Join(e.dir, "data"), "test-1", key)
	if err != nil {
		t.Fatal(err)
	}
	return mgr
}

// cryptoSignTx signs a tx (thin wrapper so tests stay import-light).
func cryptoSignTx(tx *types.Tx, priv ed25519.PrivateKey) error {
	return crypto.SignTx(tx, priv)
}
