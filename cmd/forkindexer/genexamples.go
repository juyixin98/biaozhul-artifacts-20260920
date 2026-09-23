// gen-examples writes deterministic example block streams that exercise the
// three-level fork scenario. Every block hash is computed for real with
// domain.BlockHash (SHA-256 over canonical JSON).
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"forkindexer/internal/domain"
)

const (
	alice = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	bob   = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	carol = "0xcccccccccccccccccccccccccccccccccccccccc"
	dave  = "0xdddddddddddddddddddddddddddddddddddddddd"
)

type builder struct {
	blocks map[string]*domain.Block
}

func newBuilder() *builder { return &builder{blocks: map[string]*domain.Block{}} }

func (bd *builder) makeBlock(parent string, height int64, ts []domain.Transfer) *domain.Block {
	raw, err := domain.CanonicalBody(parent, height, ts)
	must(err)
	b := &domain.Block{
		Hash:       domain.BlockHash(raw),
		ParentHash: parent,
		Height:     height,
		Transfers:  ts,
		Raw:        raw,
	}
	bd.blocks[b.Hash] = b
	return b
}

func cmdGenExamples(args []string) error {
	if len(args) != 1 {
		return fmt.Errorf("gen-examples requires <out-dir>")
	}
	dir := args[0]
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	bd := newBuilder()

	//
	// Three-level fork scenario.
	//
	//          A1 (weight2) ─ A2 (weight3)        [main until B wins]
	//        /
	//   G ── B1 (weight2) ─ B2 (weight3) ─ B3 (weight4)  <- final winner
	//                 \
	//                  C2 (weight3)  (third fork off B1)
	//
	G := bd.makeBlock(domain.ZeroHash, 0, []domain.Transfer{
		{From: alice, To: bob, Amount: 1000},
	})
	A1 := bd.makeBlock(G.Hash, 1, []domain.Transfer{{From: bob, To: carol, Amount: 100}})
	B1 := bd.makeBlock(G.Hash, 1, []domain.Transfer{{From: bob, To: dave, Amount: 40}})
	A2 := bd.makeBlock(A1.Hash, 2, []domain.Transfer{{From: carol, To: alice, Amount: 10}})
	B2 := bd.makeBlock(B1.Hash, 2, []domain.Transfer{{From: dave, To: alice, Amount: 5}})
	C2 := bd.makeBlock(B1.Hash, 2, []domain.Transfer{{From: bob, To: carol, Amount: 1}})
	B3 := bd.makeBlock(B2.Hash, 3, []domain.Transfer{{From: alice, To: bob, Amount: 200}})

	// out-of-order + orphan staging: B3 is delivered BEFORE B2 and before B1's
	// fork has connected; it must sit staged and only connect once B2 arrives.
	ordered := []*domain.Block{G, A1, B1, A2, B3 /* orphan */, B2, C2}

	// Envelopes: deterministic sequence 1..N. This stream makes A the head
	// first (heights 0..2), then B overtakes at B3 (weight 4 > 3).
	if err := writeEnvelopes(filepath.Join(dir, "stream1_three_forks.ndjson"), ordered); err != nil {
		return err
	}

	// Replay file: repeat the whole stream, plus the same envelopes again.
	// A correct indexer must not double-count anything.
	if err := writeEnvelopes(filepath.Join(dir, "stream2_replay.ndjson"), ordered); err != nil {
		return err
	}

	// Bare blocks (no envelope sequences): useful for ad-hoc seeding and
	// proving cursor stays put when blocks arrive without sequences.
	if err := writeBare(filepath.Join(dir, "stream3_bare_seed.ndjson"),
		[]*domain.Block{G, A1, B1}); err != nil {
		return err
	}

	// A deliberately bad block (declared hash does not match content).
	// Compact single-line JSON so the NDJSON ingest CLI reads it as one record.
	bad := *G
	bad.Hash = "0x" + repeat('1', 64)
	if err := os.WriteFile(filepath.Join(dir, "bad_hash_mismatch.json"),
		append(mustJSON(map[string]any{"sequence": 1, "block": blockJSON(&bad)}), '\n'), 0o644); err != nil {
		return err
	}

	// Same hash, different content: tamper parent hash but reuse G's hash.
	tampered := *G
	tampered.ParentHash = "0x" + repeat('2', 64)
	tampered.Height = 1
	if err := os.WriteFile(filepath.Join(dir, "bad_same_hash_different_content.json"),
		append(mustJSON(map[string]any{"sequence": 2, "block": blockJSON(&tampered)}), '\n'), 0o644); err != nil {
		return err
	}

	// README for the scenario.
	summary := fmt.Sprintf(`# Example streams (three-level fork)

Genesis G:
  hash        %s
  transfer    alice -> bob 1000

Main/A fork (head until overtaken):
  A1 hash     %s  (bob -> carol 100)
  A2 hash     %s  (carol -> alice 10)

B fork (final canonical chain; cumulative weight 4 wins):
  B1 hash     %s  (bob -> dave 40)
  B2 hash     %s  (dave -> alice 5)
  B3 hash     %s  (alice -> bob 200)

C fork (third level, branches off B1, ties A2/B2 at weight 3):
  C2 hash     %s  (bob -> carol 1)

Delivery order in stream1 is intentionally out of order: B3 (the future
height-3 tip) arrives BEFORE B2, so it is staged as an orphan and connected
automatically when B2 is delivered.

Final canonical chain balances after stream1 (G-B1-B2-B3):
  alice = -1000 (G) + 5 (B2) - 200 (B3) = -1195
  bob   = +1000 (G) - 40 (B1) + 200 (B3) = 1160
  dave  = +40 (B1) - 5 (B2)            = 35
  carol = 0 (her A1/A2 credits are reorged out; C2 never wins)
`, G.Hash, A1.Hash, A2.Hash, B1.Hash, B2.Hash, B3.Hash, C2.Hash)
	if err := os.WriteFile(filepath.Join(dir, "README.md"), []byte(summary), 0o644); err != nil {
		return err
	}

	fmt.Println("examples written to", dir)
	return nil
}

func repeat(c byte, n int) string {
	b := make([]byte, n)
	for i := range b {
		b[i] = c
	}
	return string(b)
}

func blockJSON(b *domain.Block) map[string]any {
	ts := make([]map[string]any, len(b.Transfers))
	for i, t := range b.Transfers {
		ts[i] = map[string]any{"from": t.From, "to": t.To, "amount": t.Amount}
	}
	return map[string]any{
		"hash":        b.Hash,
		"parent_hash": b.ParentHash,
		"height":      b.Height,
		"transfers":   ts,
	}
}

func envJSON(seq int64, b *domain.Block) []byte {
	return mustJSON(map[string]any{"sequence": seq, "block": blockJSON(b)})
}

func writeEnvelopes(path string, bs []*domain.Block) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	for i, b := range bs {
		if _, err := f.Write(append(envJSON(int64(i+1), b), '\n')); err != nil {
			return err
		}
	}
	return nil
}

func writeBare(path string, bs []*domain.Block) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	for _, b := range bs {
		if _, err := f.Write(append(mustJSON(blockJSON(b)), '\n')); err != nil {
			return err
		}
	}
	return nil
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	must(err)
	return b
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
