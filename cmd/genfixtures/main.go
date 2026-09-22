// Command genfixtures generates the example NDJSON block stream under
// testdata/. All hashes are REAL SHA-256 hashes of each block's canonical JSON
// preimage, so the files exercise the actual verification path.
//
// Scenario (delivery order differs from chain order):
//
//  1. A1 delivered while its parent is missing        -> staged
//  2. G  (genesis, mints 1000 to Alice)               -> connects
//  3. B1 (height-1 sibling fork)                      -> equal-weight tie-break
//  4. A2 (extends A1 to height 2)                     -> heavier chain reorgs
//
// Final canonical chain regardless of hash-tie outcome is G -> A1 -> A2:
//
//	Alice = 1000 - 100 + 40 = 940
//	Bob   =  100 -  40 = 60
//	Carol = 0 (her credit only exists on the orphaned fork)
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"forkindexer/internal/model"
)

func addr(hex string) string { return "0x" + hex }

func main() {
	dir := flag.String("out", "testdata", "output directory")
	flag.Parse()
	if err := os.MkdirAll(*dir, 0o755); err != nil {
		fatal(err)
	}

	alice := addr("aa00000000000000000000000000000000000001")
	bob := addr("bb00000000000000000000000000000000000002")
	carol := addr("cc00000000000000000000000000000000000003")

	// Build leaves first; hashes are computed bottom-up from real preimages.
	g, _, err := model.Normalize(model.Block{
		ParentHash: model.GenesisParent,
		Height:     0,
		Transactions: []model.Transfer{
			{From: "", To: alice, Amount: "1000"},
		},
	})
	must(err)

	a1, _, err := model.Normalize(model.Block{
		ParentHash: g.Hash,
		Height:     1,
		Transactions: []model.Transfer{
			{From: alice, To: bob, Amount: "100"},
		},
	})
	must(err)

	b1, _, err := model.Normalize(model.Block{
		ParentHash: g.Hash,
		Height:     1,
		Transactions: []model.Transfer{
			{From: alice, To: carol, Amount: "30"},
		},
	})
	must(err)

	a2, _, err := model.Normalize(model.Block{
		ParentHash: a1.Hash,
		Height:     2,
		Transactions: []model.Transfer{
			{From: bob, To: alice, Amount: "40"},
		},
	})
	must(err)

	// Delivery order: A1 arrives BEFORE genesis (orphan staging), then G
	// connects it, B1 opens an equal-weight sibling fork, A2 wins it.
	delivery := []model.Block{a1, g, b1, a2}

	streamPath := filepath.Join(*dir, "stream.ndjson")
	f, err := os.Create(streamPath)
	must(err)
	enc := json.NewEncoder(f)
	enc.SetEscapeHTML(false)
	for _, b := range delivery {
		must(enc.Encode(b))
	}
	must(f.Close())

	// Same stream again to demonstrate duplicate delivery is idempotent.
	f, err = os.Create(filepath.Join(*dir, "stream_duplicate.ndjson"))
	must(err)
	enc = json.NewEncoder(f)
	enc.SetEscapeHTML(false)
	for _, b := range append(append([]model.Block{}, delivery...), delivery...) {
		must(enc.Encode(b))
	}
	must(f.Close())

	// A valid-looking block whose stored hash will not match its content, to
	// demonstrate content/hash mismatch rejection.
	tampered := b1
	tampered.Transactions = []model.Transfer{{From: alice, To: carol, Amount: "999"}}
	// Keep B1's claimed hash so verification must fail.
	tampered.Hash = b1.Hash
	tb, err := json.Marshal(tampered)
	must(err)
	must(os.WriteFile(filepath.Join(*dir, "tampered_hash.json"), tb, 0o644))

	// Expected final state for the acceptance script.
	want := map[string]any{
		"canonicalChain": []string{g.Hash, a1.Hash, a2.Hash},
		"head":           a2.Hash,
		"headHeight":     2,
		"balances": map[string]string{
			alice: "940",
			bob:   "60",
			carol: "0",
		},
		"hashes": map[string]string{
			"G":  g.Hash,
			"A1": a1.Hash,
			"B1": b1.Hash,
			"A2": a2.Hash,
		},
		"tieBreakWinnerAtHeight1": minHash(a1.Hash, b1.Hash),
	}
	wb, err := json.MarshalIndent(want, "", "  ")
	must(err)
	must(os.WriteFile(filepath.Join(*dir, "expected.json"), wb, 0o644))

	fmt.Println("wrote fixtures to", *dir)
	fmt.Println("G :", g.Hash)
	fmt.Println("A1:", a1.Hash)
	fmt.Println("B1:", b1.Hash)
	fmt.Println("A2:", a2.Hash)
	fmt.Println("height-1 tie-break winner:", minHash(a1.Hash, b1.Hash))
}

func minHash(a, b string) string {
	if a < b {
		return a
	}
	return b
}

func must(err error) {
	if err != nil {
		fatal(err)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
