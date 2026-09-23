// Package enginetest contains the end-to-end test suite for snapshot
// construction, concurrent history queries, interrupted builds, pruning
// races, lease expiry and independent replay cross-checks.
package enginetest

import (
	"crypto/ed25519"
	"path/filepath"
	"testing"
	"time"

	"github.com/example/snapshotprune/internal/crypto"
	"github.com/example/snapshotprune/internal/engine"
	"github.com/example/snapshotprune/internal/types"
)

// fakeClock is a manually-advanced Clock.
type fakeClock struct{ t time.Time }

func (c *fakeClock) Now() time.Time          { return c.t }
func (c *fakeClock) Advance(d time.Duration) { c.t = c.t.Add(d) }

// testEnv is an opened engine with deterministic accounts and a fake clock.
type testEnv struct {
	t     *testing.T
	dir   string
	eng   *engine.Engine
	clock *fakeClock
	privs map[string]ed25519.PrivateKey
	addrs map[string]types.Address
}

func genesisFor(addrs map[string]types.Address) *types.Genesis {
	g := &types.Genesis{ChainID: "test-1"}
	for _, a := range addrs {
		g.Allocations = append(g.Allocations, types.GenesisAlloc{Address: a.Hex(), Balance: 1_000_000})
	}
	return g
}

func newEnv(t *testing.T) *testEnv {
	t.Helper()
	dir := t.TempDir()
	privs := map[string]ed25519.PrivateKey{}
	addrs := map[string]types.Address{}
	for _, seed := range []string{"alice", "bob", "carol"} {
		k := crypto.DemoPriv(seed)
		a, err := crypto.AddressFromPub(k.Public().(ed25519.PublicKey))
		if err != nil {
			t.Fatal(err)
		}
		privs[seed] = k
		addrs[seed] = a
	}
	clk := &fakeClock{t: time.Unix(1_700_000_000, 0)}
	eng, err := engine.Open(engine.Config{
		DataDir:  filepath.Join(dir, "data"),
		ChainID:  "test-1",
		Genesis:  genesisFor(addrs),
		LeaseTTL: time.Hour,
	})
	if err != nil {
		t.Fatal(err)
	}
	eng.SetClock(clk)
	e := &testEnv{
		t: t, dir: dir, eng: eng, clock: clk, privs: privs, addrs: addrs,
	}
	t.Cleanup(func() { _ = eng.Close() })
	return e
}

// appendN appends n blocks; block i transfers 100+1 fee from alice to a
// rotating recipient, exercising signature verification each time.
func (e *testEnv) appendN(n int) {
	e.t.Helper()
	tip, err := e.eng.Tip()
	if err != nil {
		e.t.Fatal(err)
	}
	for i := 1; i <= n; i++ {
		to := e.addrs["bob"]
		if i%2 == 0 {
			to = e.addrs["carol"]
		}
		// Read alice nonce through the query path.
		tbl, err := e.eng.Reconstruct(tip)
		if err != nil {
			e.t.Fatal(err)
		}
		var nonce uint64
		if acc := tbl[e.addrs["alice"]]; acc != nil {
			nonce = acc.Nonce
		}
		tx := types.Tx{Nonce: nonce, To: to, Amount: 100, Fee: 1}
		if err := crypto.SignTx(&tx, e.privs["alice"]); err != nil {
			e.t.Fatal(err)
		}
		p := &engine.Proposal{
			Height:    tip + 1,
			Timestamp: e.clock.Now().UnixNano(),
			Txs:       []types.Tx{tx},
		}
		if _, err := e.eng.AppendProposal(p); err != nil {
			e.t.Fatalf("append %d: %v", tip+1, err)
		}
		tip++
	}
}

func (e *testEnv) mustReopen() {
	e.t.Helper()
	if err := e.eng.Close(); err != nil {
		e.t.Fatal(err)
	}
	eng, err := engine.Open(engine.Config{
		DataDir:  filepath.Join(e.dir, "data"),
		ChainID:  "test-1",
		LeaseTTL: time.Hour,
	})
	if err != nil {
		e.t.Fatal(err)
	}
	eng.SetClock(e.clock)
	e.eng = eng
}
