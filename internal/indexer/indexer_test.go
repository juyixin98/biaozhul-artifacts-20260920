package indexer_test

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"math/big"
	"os"
	"sync"
	"sync/atomic"
	"testing"

	"forkindexer/internal/indexer"
	"forkindexer/internal/model"
	"forkindexer/internal/store"

	"github.com/jackc/pgx/v5/pgxpool"
)

// testIndexer builds an isolated schema per test inside the test database.
func testIndexer(t *testing.T) (*indexer.Indexer, *pgxpool.Pool) {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		dsn = "host=/var/run/postgresql user=admin dbname=forkindexer_test"
	}
	schema := newSchemaName()
	base, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Skipf("cannot parse TEST_DATABASE_URL %q: %v", dsn, err)
	}
	base.ConnConfig.RuntimeParams["search_path"] = schema
	ctx := context.Background()
	admin, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Skipf("no test database reachable at %q (run `make test-setup`): %v", dsn, err)
	}
	defer admin.Close()
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		t.Fatalf("create schema: %v", err)
	}
	t.Cleanup(func() {
		_, _ = admin.Exec(ctx, "DROP SCHEMA "+schema+" CASCADE")
	})

	pool, err := pgxpool.NewWithConfig(ctx, base)
	if err != nil {
		t.Fatalf("pool: %v", err)
	}
	t.Cleanup(pool.Close)
	if err := store.Migrate(ctx, pool); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	return indexer.New(pool), pool
}

var schemaCounter int64
var schemaPrefix = func() string {
	var b [4]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}()

func nextSchemaID() int {
	return int(atomic.AddInt64(&schemaCounter, 1))
}

func newSchemaName() string {
	return fmt.Sprintf("t_%s_%d", schemaPrefix, nextSchemaID())
}

// chainBuilder constructs blocks with real SHA-256 hashes. It is safe for
// concurrent use: the concurrent test extends independent branches in
// parallel.
type chainBuilder struct {
	mu     sync.Mutex
	blocks map[string]model.Block
}

func newChainBuilder() *chainBuilder { return &chainBuilder{blocks: map[string]model.Block{}} }

func addr(name byte) string {
	out := make([]byte, 40)
	for i := range out {
		out[i] = '0'
	}
	out[39] = name
	return "0x" + string(out)
}

func (c *chainBuilder) block(parent model.Block, height int64, txs ...model.Transfer) model.Block {
	parentHash := model.GenesisParent
	if parent.Hash != "" {
		parentHash = parent.Hash
	}
	b, _, err := model.Normalize(model.Block{ParentHash: parentHash, Height: height, Transactions: txs})
	if err != nil {
		panic(err)
	}
	c.mu.Lock()
	c.blocks[b.Hash] = b
	c.mu.Unlock()
	return b
}

func (c *chainBuilder) genesis(txs ...model.Transfer) model.Block {
	return c.block(model.Block{}, 0, txs...)
}

func mustIngest(t *testing.T, ix *indexer.Indexer, blocks ...model.Block) *indexer.IngestResult {
	t.Helper()
	res, err := ix.Ingest(context.Background(), blocks, nil)
	if err != nil {
		t.Fatalf("ingest: %v", err)
	}
	return res
}

func expectBalance(t *testing.T, ix *indexer.Indexer, addr, want string) {
	t.Helper()
	got, err := ix.Balance(context.Background(), addr)
	if err != nil {
		t.Fatalf("balance: %v", err)
	}
	if got != want {
		t.Errorf("balance %s = %s, want %s", addr, got, want)
	}
}

func expectHead(t *testing.T, ix *indexer.Indexer, want string, height int64) {
	t.Helper()
	h, err := ix.Head(context.Background())
	if err != nil {
		t.Fatalf("head: %v", err)
	}
	if h == nil || h.Hash != want || h.Height != height {
		got := "<none>"
		if h != nil {
			got = fmt.Sprintf("%s height=%d", h.Hash, h.Height)
		}
		t.Errorf("head = %s, want %s height=%d", got, want, height)
	}
}

func verifyMatch(t *testing.T, ix *indexer.Indexer) *indexer.RebuildReport {
	t.Helper()
	rep, err := ix.VerifyAgainstRebuild(context.Background())
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if !rep.Match {
		t.Fatalf("incremental state != genesis rebuild:\n%v", rep.Mismatches)
	}
	return rep
}

func expectBadBlock(t *testing.T, err error) {
	t.Helper()
	var bad *indexer.BadBlockError
	if !errors.As(err, &bad) {
		t.Fatalf("expected BadBlockError, got %T: %v", err, err)
	}
}

// TestOrphanStagingAndConnect: a block arriving before its parent is staged,
// then connects once the parent chain completes.
func TestOrphanStagingAndConnect(t *testing.T) {
	ix, _ := testIndexer(t)
	cb := newChainBuilder()
	a, b := addr('a'), addr('b')

	g := cb.genesis(model.Transfer{From: "", To: a, Amount: "100"})
	a1 := cb.block(g, 1, model.Transfer{From: a, To: b, Amount: "30"})
	a2 := cb.block(a1, 2, model.Transfer{From: b, To: a, Amount: "10"})

	res := mustIngest(t, ix, a2) // deepest first -> staged
	if got := res.Blocks[0].Status; got != model.StatusStaged {
		t.Fatalf("a2 status = %s, want staged", got)
	}
	if h, _ := ix.Head(context.Background()); h != nil {
		t.Fatalf("head should not exist while only an orphan is stored, got %v", h)
	}

	mustIngest(t, ix, g) // a2 still staged (missing a1)
	mustIngest(t, ix, a1)
	expectHead(t, ix, a2.Hash, 2)
	expectBalance(t, ix, a, "80")
	expectBalance(t, ix, b, "20")
	verifyMatch(t, ix)
}

// TestThreeLevelFork exercises forks at three successive heights, reorgs
// across levels and equal-weight branches resolved strictly by tip hash.
func TestThreeLevelFork(t *testing.T) {
	ix, _ := testIndexer(t)
	cb := newChainBuilder()
	a, b, c, d := addr('a'), addr('b'), addr('c'), addr('d')

	g := cb.genesis(model.Transfer{From: "", To: a, Amount: "1000"})

	// Level 1 fork at height 1.
	x := cb.block(g, 1, model.Transfer{From: a, To: b, Amount: "100"})
	y := cb.block(g, 1, model.Transfer{From: a, To: c, Amount: "200"})
	// Level 2 fork at height 2.
	x1 := cb.block(x, 2, model.Transfer{From: b, To: a, Amount: "10"})
	y1 := cb.block(y, 2, model.Transfer{From: c, To: a, Amount: "20"})
	// Level 3 fork at height 3.
	x2 := cb.block(x1, 3, model.Transfer{From: a, To: d, Amount: "5"})
	y2 := cb.block(y1, 3, model.Transfer{From: a, To: d, Amount: "7"})

	mustIngest(t, ix, g)
	mustIngest(t, ix, x)
	mustIngest(t, ix, y)
	want1 := minHash(x.Hash, y.Hash)
	h, _ := ix.Head(context.Background())
	if h.Hash != want1 {
		t.Fatalf("height-1 tie-break: head %s want %s", h.Hash, want1)
	}

	mustIngest(t, ix, x1)
	mustIngest(t, ix, y1)
	want2 := minHash(x1.Hash, y1.Hash)
	expectHead(t, ix, want2, 2)

	// X branch pulls ahead at height 3: guaranteed reorg onto X.
	mustIngest(t, ix, x2)
	expectHead(t, ix, x2.Hash, 3)

	// Y branch catches up (3) and overtakes to height 4: full three-level
	// reorg back onto Y.
	y3 := cb.block(y2, 4, model.Transfer{From: d, To: c, Amount: "1"})
	r := mustIngest(t, ix, y2, y3)
	if r.Reorg == nil {
		t.Fatal("expected reorg onto longer Y branch")
	}
	expectHead(t, ix, y3.Hash, 4)

	// Replaying G->Y->Y1->Y2->Y3 from genesis:
	// a: 1000-200+20-7 = 813 ; c: 200-20+1 = 181 ; d: 7-1 = 6 ; b: 0
	expectBalance(t, ix, a, "813")
	expectBalance(t, ix, b, "0")
	expectBalance(t, ix, c, "181")
	expectBalance(t, ix, d, "6")

	chain, err := ix.Chain(context.Background(), "")
	if err != nil || len(chain) != 5 {
		t.Fatalf("chain depth = %d (err=%v), want 5", len(chain), err)
	}
	if chain[0].Hash != y3.Hash || chain[4].Hash != g.Hash {
		t.Errorf("chain order wrong: tip=%s genesis=%s", chain[0].Hash, chain[4].Hash)
	}
	verifyMatch(t, ix)
}

// TestTieBreakOnlyByHash drives a pure equal-weight decision and checks the
// winner exactly matches lexicographic hash comparison.
func TestTieBreakOnlyByHash(t *testing.T) {
	ix, _ := testIndexer(t)
	cb := newChainBuilder()
	a := addr('a')
	g := cb.genesis(model.Transfer{From: "", To: a, Amount: "1000"})
	mustIngest(t, ix, g)

	var tips []model.Block
	for i := 0; i < 5; i++ {
		who := addr(byte('b' + i))
		// Distinct amount per tip -> distinct content -> distinct hash.
		tips = append(tips, cb.block(g, 1, model.Transfer{From: a, To: who, Amount: fmt.Sprint(i + 1)}))
	}
	for _, blk := range tips {
		mustIngest(t, ix, blk)
	}
	want := tips[0].Hash
	for _, blk := range tips[1:] {
		want = minHash(want, blk.Hash)
	}
	expectHead(t, ix, want, 1)
	verifyMatch(t, ix)
}

// TestDuplicateDelivery: redelivering identical blocks never double-counts.
func TestDuplicateDelivery(t *testing.T) {
	ix, _ := testIndexer(t)
	cb := newChainBuilder()
	a, b := addr('a'), addr('b')

	g := cb.genesis(model.Transfer{From: "", To: a, Amount: "500"})
	a1 := cb.block(g, 1, model.Transfer{From: a, To: b, Amount: "50"})
	a2 := cb.block(a1, 2, model.Transfer{From: b, To: a, Amount: "20"})

	mustIngest(t, ix, g, a1, a2)
	st, _ := ix.State(context.Background())
	if st.IngestSeq != 3 {
		t.Fatalf("ingestSeq = %d, want 3", st.IngestSeq)
	}
	for i := 0; i < 3; i++ {
		res := mustIngest(t, ix, a2, a1, g, g)
		if res.Accepted != 0 || res.Duplicates != 4 || res.Reorg != nil {
			t.Fatalf("round %d: accepted=%d duplicates=%d reorg=%v", i, res.Accepted, res.Duplicates, res.Reorg)
		}
	}
	st, _ = ix.State(context.Background())
	if st.IngestSeq != 3 {
		t.Fatalf("ingestSeq drifted after duplicates: %d", st.IngestSeq)
	}
	expectBalance(t, ix, a, "470")
	expectBalance(t, ix, b, "30")
	verifyMatch(t, ix)
}

// TestSameHashDifferentContent rejects a claimed hash whose preimage differs.
func TestSameHashDifferentContent(t *testing.T) {
	ix, _ := testIndexer(t)
	cb := newChainBuilder()
	a := addr('a')
	g := cb.genesis(model.Transfer{From: "", To: a, Amount: "100"})
	mustIngest(t, ix, g)

	forge := model.Block{
		Hash:       g.Hash, // lie about the identity
		ParentHash: model.GenesisParent,
		Height:     0,
		Transactions: []model.Transfer{
			{From: "", To: a, Amount: "999999999"},
		},
	}
	_, err := ix.Ingest(context.Background(), []model.Block{forge}, nil)
	if err == nil {
		t.Fatal("expected rejection of same-hash/different-content block")
	}
	expectBadBlock(t, err)

	// Stored block with identical canonical content is a benign duplicate.
	res := mustIngest(t, ix, g)
	if res.Duplicates != 1 {
		t.Fatalf("honest duplicate not recognized: %+v", res)
	}
	expectBalance(t, ix, a, "100")
}

// TestContentHashIsRealSHA256: tampering after signing must fail with a
// mismatch naming the actually-computed SHA-256 of the new preimage.
func TestContentHashIsRealSHA256(t *testing.T) {
	ix, _ := testIndexer(t)
	a := addr('a')
	b, _, err := model.Normalize(model.Block{
		Height: 0, Transactions: []model.Transfer{{From: "", To: a, Amount: "1"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	b.Transactions[0].Amount = "2" // mutate post-hash; claimed hash now stale
	_, err = ix.Ingest(context.Background(), []model.Block{b}, nil)
	if err == nil {
		t.Fatal("stale hash after mutation must be rejected")
	}
	expectBadBlock(t, err)
}

// TestOverdraftInvalidatesTip: an unfundable tip is invalidated with its
// descendants; a competing funded branch wins; the canonical ledger never
// goes negative.
func TestOverdraftInvalidatesTip(t *testing.T) {
	ix, _ := testIndexer(t)
	cb := newChainBuilder()
	a, b := addr('a'), addr('b')

	g := cb.genesis(model.Transfer{From: "", To: a, Amount: "10"})
	bad := cb.block(g, 1, model.Transfer{From: a, To: b, Amount: "100"})
	good := cb.block(g, 1, model.Transfer{From: a, To: b, Amount: "10"})
	good2 := cb.block(good, 2, model.Transfer{From: b, To: a, Amount: "1"})

	mustIngest(t, ix, g, bad)
	expectHead(t, ix, g.Hash, 0)
	bi, err := ix.GetBlock(context.Background(), bad.Hash)
	if err != nil || bi.Status != model.StatusInvalid {
		t.Fatalf("bad block status = %+v err=%v, want invalid", bi, err)
	}

	mustIngest(t, ix, good, good2)
	expectHead(t, ix, good2.Hash, 2)
	expectBalance(t, ix, a, "1")
	expectBalance(t, ix, b, "9")

	badChild := cb.block(bad, 2)
	mustIngest(t, ix, badChild)
	bci, _ := ix.GetBlock(context.Background(), badChild.Hash)
	if bci.Status != model.StatusInvalid {
		t.Fatalf("descendant of invalid tip = %s, want invalid", bci.Status)
	}
	verifyMatch(t, ix)
}

// TestHeightLinkageBroken covers both linkage-check paths.
func TestHeightLinkageBroken(t *testing.T) {
	ix, _ := testIndexer(t)
	cb := newChainBuilder()
	a := addr('a')
	g := cb.genesis(model.Transfer{From: "", To: a, Amount: "10"})
	mustIngest(t, ix, g)

	// Known parent, wrong child height: rejected immediately.
	wrong, _, err := model.Normalize(model.Block{ParentHash: g.Hash, Height: 5})
	if err != nil {
		t.Fatal(err)
	}
	_, err = ix.Ingest(context.Background(), []model.Block{wrong}, nil)
	expectBadBlock(t, err)

	// Staged orphan whose later-arriving parent sits at the wrong height:
	// orphan gets invalidated by the fixpoint pass.
	g2 := cb.genesis()
	orphan, _, err := model.Normalize(model.Block{ParentHash: g2.Hash, Height: 3})
	if err != nil {
		t.Fatal(err)
	}
	res := mustIngest(t, ix, orphan) // parent g2 unknown -> staged
	if res.Blocks[0].Status != model.StatusStaged {
		t.Fatalf("orphan status = %s, want staged", res.Blocks[0].Status)
	}
	mustIngest(t, ix, g2) // parent at height 0 but child claims 3 -> invalid
	oi, _ := ix.GetBlock(context.Background(), orphan.Hash)
	if oi.Status != model.StatusInvalid {
		t.Fatalf("orphan after bad-parent connect = %s, want invalid", oi.Status)
	}
	verifyMatch(t, ix)
}

// TestConcurrentReorgConsistency: many branches ingested concurrently while
// readers continuously snapshot. Writers serialize on the advisory lock;
// every committed state must equal a from-genesis rebuild.
func TestConcurrentReorgConsistency(t *testing.T) {
	ix, _ := testIndexer(t)
	cb := newChainBuilder()
	a := addr('a')
	g := cb.genesis(model.Transfer{From: "", To: a, Amount: "1000000"})
	mustIngest(t, ix, g)

	const branches, depth = 8, 6
	var writerWG, readerWG sync.WaitGroup
	var readerStop atomic.Bool
	errCh := make(chan error, 64)

	readerWG.Add(1)
	go func() {
		defer readerWG.Done()
		for !readerStop.Load() {
			// One repeatable-read snapshot: head, chain depth, canonical row
			// count and a balance must all describe the SAME committed chain.
			snap, err := ix.ConsistencyProbe(context.Background(), a)
			if err != nil {
				errCh <- err
				return
			}
			if snap.HeadHash == "" {
				continue
			}
			if snap.Depth != int(snap.HeadHeight)+1 {
				errCh <- fmt.Errorf("mixed read: head height %d but chain depth %d", snap.HeadHeight, snap.Depth)
				return
			}
			v, ok := new(big.Int).SetString(snap.Balance, 10)
			if !ok || v.Sign() < 0 {
				errCh <- fmt.Errorf("impossible balance %q", snap.Balance)
				return
			}
		}
	}()

	for i := 0; i < branches; i++ {
		writerWG.Add(1)
		go func() {
			defer writerWG.Done()
			cur := g
			for d := 1; d <= depth; d++ {
				nxt := cb.block(cur, int64(d), model.Transfer{From: a, To: a, Amount: "1"})
				if _, err := ix.Ingest(context.Background(), []model.Block{nxt}, nil); err != nil {
					errCh <- err
					return
				}
				cur = nxt
			}
		}()
	}
	writerWG.Wait()
	readerStop.Store(true)
	readerWG.Wait()

	select {
	case err := <-errCh:
		t.Fatalf("concurrent failure: %v", err)
	default:
	}
	h, _ := ix.Head(context.Background())
	if h.Height != depth {
		t.Fatalf("final head height = %d, want %d", h.Height, depth)
	}
	verifyMatch(t, ix)
}

// TestLargeIntegerAmounts: amounts beyond 2^64 preserve exact precision.
func TestLargeIntegerAmounts(t *testing.T) {
	ix, _ := testIndexer(t)
	cb := newChainBuilder()
	a, b := addr('a'), addr('b')
	huge := new(big.Int).Lsh(big.NewInt(1), 200) // 2^200
	g := cb.genesis(model.Transfer{From: "", To: a, Amount: huge.String()})
	rest := new(big.Int).Sub(huge, big.NewInt(7))
	a1 := cb.block(g, 1, model.Transfer{From: a, To: b, Amount: rest.String()})
	mustIngest(t, ix, g, a1)
	expectBalance(t, ix, a, "7")
	expectBalance(t, ix, b, rest.String())
	verifyMatch(t, ix)
}

// TestMintAndBurn: empty from mints, empty to burns.
func TestMintAndBurn(t *testing.T) {
	ix, _ := testIndexer(t)
	cb := newChainBuilder()
	a := addr('a')
	g := cb.genesis(
		model.Transfer{From: "", To: a, Amount: "100"},
		model.Transfer{From: "", To: "", Amount: "0"},
	)
	burn := cb.block(g, 1, model.Transfer{From: a, To: "", Amount: "40"})
	mint := cb.block(burn, 2, model.Transfer{From: "", To: a, Amount: "5"})
	mustIngest(t, ix, g, burn, mint)
	expectBalance(t, ix, a, "65")
	verifyMatch(t, ix)
}

// TestUnrelatedGenesisChains: two independent genesis trees compete solely by
// weight/tie-break; switching fully undoes one and applies the other.
func TestUnrelatedGenesisChains(t *testing.T) {
	ix, _ := testIndexer(t)
	cb := newChainBuilder()
	a, b := addr('a'), addr('b')

	g1 := cb.genesis(model.Transfer{From: "", To: a, Amount: "100"})
	g2 := cb.genesis(model.Transfer{From: "", To: b, Amount: "200"})

	mustIngest(t, ix, g1)
	expectHead(t, ix, g1.Hash, 0)
	mustIngest(t, ix, g2) // equal weight, hash tie-break
	want := minHash(g1.Hash, g2.Hash)
	expectHead(t, ix, want, 0)

	// Extend whichever genesis lost (or won) — g1 gets height 1 and wins by
	// weight regardless of hashes, fully undoing g2's mint if it was head.
	g1c := cb.block(g1, 1)
	mustIngest(t, ix, g1c)
	expectHead(t, ix, g1c.Hash, 1)
	expectBalance(t, ix, a, "100")
	expectBalance(t, ix, b, "0")
	verifyMatch(t, ix)
}

func minHash(x, y string) string {
	if x < y {
		return x
	}
	return y
}
