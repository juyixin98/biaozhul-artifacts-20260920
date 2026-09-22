package indexer_test

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"math/big"
	"math/rand"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"

	"forkindexer/internal/indexer"
	"forkindexer/internal/model"
	"forkindexer/internal/store"

	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	indexerBinOnce sync.Once
	indexerBinPath string
	indexerBinErr  error
)

func buildIndexerBinary() (string, error) {
	indexerBinOnce.Do(func() {
		dir, err := os.MkdirTemp("", "forkindexer-bin")
		if err != nil {
			indexerBinErr = err
			return
		}
		out := filepath.Join(dir, "indexer")
		// Import path, not ./cmd/indexer: tests execute with package dir as
		// cwd, but module resolution finds the package either way.
		cmd := exec.Command("go", "build", "-o", out, "forkindexer/cmd/indexer")
		indexerBinErr = cmd.Run()
		indexerBinPath = out
	})
	return indexerBinPath, indexerBinErr
}

// isolatedSchema creates a fresh schema and returns a runtime DSN pinned to it
// (for child processes) plus a pool on the same schema.
func isolatedSchema(t *testing.T) (string, *pgxpool.Pool) {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		dsn = "host=/var/run/postgresql user=admin dbname=forkindexer_test"
	}
	schema := newSchemaName()
	admin, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Skipf("test database unreachable at %q: %v", dsn, err)
	}
	if _, err := admin.Exec(context.Background(), "CREATE SCHEMA "+schema); err != nil {
		t.Fatalf("create schema: %v", err)
	}
	t.Cleanup(func() {
		_, _ = admin.Exec(context.Background(), "DROP SCHEMA "+schema+" CASCADE")
		admin.Close()
	})

	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	cfg.ConnConfig.RuntimeParams["search_path"] = schema
	pool, err := pgxpool.NewWithConfig(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Migrate(context.Background(), pool); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	return dsn + " search_path=" + schema, pool
}

// TestCrashRestartAndCursor runs the real indexer binary against a block
// stream, terminating it abruptly (os.Exit via --exit-after) three times
// mid-stream and restarting from the durable byte-offset cursor each time.
// Final state must equal an independent replay and a from-genesis rebuild.
func TestCrashRestartAndCursor(t *testing.T) {
	bin, err := buildIndexerBinary()
	if err != nil {
		t.Fatalf("build binary: %v", err)
	}
	runtimeDSN, pool := isolatedSchema(t)
	ix := indexer.New(pool)

	cb := newChainBuilder()
	a, b := addr('a'), addr('b')
	g := cb.genesis(model.Transfer{From: "", To: a, Amount: "1000000"})
	cur := g
	stream := []model.Block{g}
	for d := 1; d <= 60; d++ {
		var txs []model.Transfer
		// Valid funding order in every cycle of 3: a funds b, a funds b
		// again, only then does b spend back. b can never overdraft.
		switch d % 3 {
		case 1:
			txs = []model.Transfer{{From: a, To: b, Amount: "3"}}
		case 2:
			txs = []model.Transfer{{From: a, To: b, Amount: "1"}}
		case 0:
			txs = []model.Transfer{{From: b, To: a, Amount: "2"}}
		}
		cur = cb.block(cur, int64(d), txs...)
		stream = append(stream, cur)
	}
	dir := t.TempDir()
	streamPath := filepath.Join(dir, "stream.ndjson")
	f, err := os.Create(streamPath)
	if err != nil {
		t.Fatal(err)
	}
	w := bufio.NewWriter(f)
	for _, blk := range stream {
		raw, _ := json.Marshal(blk)
		_, _ = w.Write(raw)
		_ = w.WriteByte('\n')
	}
	if err := w.Flush(); err != nil {
		t.Fatal(err)
	}
	f.Close()

	run := func(args ...string) {
		t.Helper()
		cmd := exec.Command(bin, append([]string{"ingest-file", "--file", streamPath, "--batch-size", "7"}, args...)...)
		cmd.Env = append(os.Environ(), "DATABASE_URL="+runtimeDSN)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("indexer %v failed: %v\n%s", args, err, out)
		}
	}

	// Batch size 7, stream has 61 lines (genesis + 60). A process rescans
	// from the start, so its batch counter includes leading all-duplicate
	// batches:
	//   run1: batch1 = 7 new commits; crash before batch2        -> seq 7
	//   run2: batch1 dups; batches 2..4 = 21 new; crash before batch 5 -> seq 28
	//   run3: batches 1..4 dups; batches 5..9 = 33 new to EOF      -> seq 61
	run("--exit-before-batch", "2")
	if st, _ := ix.State(context.Background()); st.IngestSeq != 7 || st.StreamOffset == 0 {
		t.Fatalf("after first crash: %+v", st)
	}
	run("--exit-before-batch", "5")
	if st, _ := ix.State(context.Background()); st.IngestSeq != 28 {
		t.Fatalf("after second crash: seq=%d want 28 (7 + 21)", st.IngestSeq)
	}
	run() // complete the stream
	st, err := ix.State(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	// 61 blocks total: one genesis at height 0 plus 60 at heights 1..60.
	if st.IngestSeq != 61 {
		t.Fatalf("final ingestSeq = %d, want 61", st.IngestSeq)
	}

	// Re-run the whole stream: every block already stored -> no double count.
	// The cursor sits at EOF and stays there.
	run()
	st2, _ := ix.State(context.Background())
	if st2.IngestSeq != 61 || st2.StreamOffset != st.StreamOffset {
		t.Fatalf("dup re-run changed durable state: %+v", st2)
	}

	// Independent expectation for the valid a-funds-b-first transfer cycle:
	// per cycle of 3: a sends 3, a sends 1, b sends 2 -> a -2, b +2.
	wantA, wantB := big.NewInt(1000000), big.NewInt(0)
	for d := 1; d <= 60; d++ {
		switch d % 3 {
		case 1:
			wantA.Sub(wantA, big.NewInt(3))
			wantB.Add(wantB, big.NewInt(3))
		case 2:
			wantA.Sub(wantA, big.NewInt(1))
			wantB.Add(wantB, big.NewInt(1))
		case 0:
			wantB.Sub(wantB, big.NewInt(2))
			wantA.Add(wantA, big.NewInt(2))
		}
	}
	t.Logf("crash/restart runs completed; final seq=%d", st.IngestSeq)
	expectBalance(t, ix, a, wantA.String())
	expectBalance(t, ix, b, wantB.String())
	expectHead(t, ix, cur.Hash, 60)
	verifyMatch(t, ix)
}

// TestShuffledDeliveryEquivalence ingests a forked block DAG in many random
// orders: the final canonical chain must be order-independent and equal to a
// from-genesis rebuild every time.
func TestShuffledDeliveryEquivalence(t *testing.T) {
	for _, seed := range []int64{1, 2, 42, 99, 2026} {
		t.Run(fmt.Sprintf("seed=%d", seed), func(t *testing.T) {
			ix, _ := testIndexer(t)
			cb := newChainBuilder()
			a, b, c := addr('a'), addr('b'), addr('c')

			g := cb.genesis(model.Transfer{From: "", To: a, Amount: "10000"})
			all := []model.Block{g}
			mk := func(length int, to string, amt int64) {
				cur := g
				for d := 1; d <= length; d++ {
					cur = cb.block(cur, int64(d), model.Transfer{From: a, To: to, Amount: fmt.Sprint(amt)})
					all = append(all, cur)
				}
			}
			mk(4, b, 10)
			mk(5, c, 7) // heaviest
			mk(3, b, 3)

			rng := rand.New(rand.NewSource(seed))
			rng.Shuffle(len(all), func(i, j int) { all[i], all[j] = all[j], all[i] })
			for len(all) > 0 {
				n := 1 + rng.Intn(4)
				if n > len(all) {
					n = len(all)
				}
				mustIngest(t, ix, all[:n]...)
				all = all[n:]
			}
			h, err := ix.Head(context.Background())
			if err != nil || h == nil || h.Height != 5 {
				t.Fatalf("head = %v err=%v, want height 5", h, err)
			}
			rep := verifyMatch(t, ix)
			if len(rep.CanonicalHashes) != 6 {
				t.Fatalf("canonical chain length = %d, want 6", len(rep.CanonicalHashes))
			}
		})
	}
}

// TestDurableStateAcrossPoolReopen drops and recreates the connection pool
// (process-restart equivalent without OS processes): cursor, head and
// balances survive and match a rebuild.
func TestDurableStateAcrossPoolReopen(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		dsn = "host=/var/run/postgresql user=admin dbname=forkindexer_test"
	}
	schema := newSchemaName()
	admin, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Skipf("test database unreachable: %v", err)
	}
	if _, err := admin.Exec(context.Background(), "CREATE SCHEMA "+schema); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, _ = admin.Exec(context.Background(), "DROP SCHEMA "+schema+" CASCADE")
		admin.Close()
	})

	openPool := func() *pgxpool.Pool {
		cfg, err := pgxpool.ParseConfig(dsn)
		if err != nil {
			t.Fatal(err)
		}
		cfg.ConnConfig.RuntimeParams["search_path"] = schema
		p, err := pgxpool.NewWithConfig(context.Background(), cfg)
		if err != nil {
			t.Fatal(err)
		}
		if err := store.Migrate(context.Background(), p); err != nil {
			t.Fatal(err)
		}
		return p
	}

	p1 := openPool()
	cb := newChainBuilder()
	a, b := addr('a'), addr('b')
	g := cb.genesis(model.Transfer{From: "", To: a, Amount: "10"})
	n1 := cb.block(g, 1, model.Transfer{From: a, To: b, Amount: "4"})
	n2 := cb.block(n1, 2)
	ix1 := indexer.New(p1)
	off := int64(456)
	if _, err := ix1.Ingest(context.Background(), []model.Block{g, n1, n2}, &off); err != nil {
		t.Fatal(err)
	}
	p1.Close() // kill every connection

	p2 := openPool()
	defer p2.Close()
	ix2 := indexer.New(p2)
	st, err := ix2.State(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if st.IngestSeq != 3 || st.StreamOffset != 456 || st.HeadHash != n2.Hash {
		t.Fatalf("state did not survive reopen: %+v", st)
	}
	expectBalance(t, ix2, a, "6")
	expectBalance(t, ix2, b, "4")
	verifyMatch(t, ix2)
}
