// Command genchain builds the canonical, trusted hash chain offline and writes
// it as the golden sample JSON. This file is the single source of truth used
// by the stub nodes (to serve) and the synchronizer (to verify); it is never
// derived from a running node.
package main

import (
	"encoding/hex"
	"flag"
	"fmt"
	"os"
	"time"

	"nodesync/internal/gen"
	"nodesync/internal/sample"
)

func main() {
	length := flag.Int("length", 32, "number of non-genesis blocks (tip height)")
	seedHex := flag.String("seed", "6e6f646573796e632d67656e65736973", "genesis seed (hex)")
	nonceHex := flag.String("nonce", "70303036", "body nonce (hex)")
	out := flag.String("out", "testdata/trusted_sample.json", "output path")
	flag.Parse()

	seed, err := decodeHex(*seedHex, "seed")
	if err != nil {
		fatal(err)
	}
	nonce, err := decodeHex(*nonceHex, "nonce")
	if err != nil {
		fatal(err)
	}
	if *length < 1 {
		fatal(fmt.Errorf("--length must be >= 1"))
	}

	blocks := gen.Build(gen.Spec{Length: *length, Seed: seed, BodyNonce: nonce})
	s := sample.FromBlocks(blocks, seed, nonce, time.Now().UTC().Format(time.RFC3339))
	if err := s.Save(*out); err != nil {
		fatal(err)
	}
	fmt.Printf("wrote trusted sample: %s\n", *out)
	fmt.Printf("  genesis hash : %s\n", s.GenesisHash)
	fmt.Printf("  tip height   : %d\n", s.TipHeight)
	fmt.Printf("  tip hash     : %s\n", s.TipHash)
	fmt.Printf("  chain digest : %s\n", s.Digest)
}

func decodeHex(h, name string) ([]byte, error) {
	b, err := hex.DecodeString(h)
	if err != nil {
		return nil, fmt.Errorf("invalid --%s: %w", name, err)
	}
	return b, nil
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "genchain:", err)
	os.Exit(1)
}
