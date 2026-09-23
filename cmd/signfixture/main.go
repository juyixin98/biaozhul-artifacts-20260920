// ximbox-signfixture is the LOCAL relayer/source-chain simulator tooling.
//
// It is NOT part of the trusted server: the server only knows the fixtures'
// PUBLIC keys and verifies signatures. This tool holds the corresponding
// (publicly known, deterministic) private keys and signs message envelopes.
//
// Usage:
//
//	ximbox-signfixture keys
//	ximbox-signfixture blockhash --chain chainA --height 1 --parent 0x0
//	ximbox-signfixture sign --chain chainA --channel chan1 --seq 0 \
//	    --block 0x<hash> --body '{"type":"transfer","to":"alice","amount":10}'
//
// Output of `sign` is the exact JSON document to POST to /v1/messages.
package main

import (
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"ximbox/internal/cryptoenvelope"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "keys":
		cmdKeys()
	case "blockhash":
		cmdBlockHash(os.Args[2:])
	case "sign":
		cmdSign(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n", os.Args[1])
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `ximbox-signfixture - local source-chain simulator

commands:
  keys                          print trusted public keys for chainA/chainB
  blockhash --chain C --height N --parent H
                                derive the deterministic simulated block hash
  sign --chain C --channel CH --seq N --block H --body JSON
                                produce a signed message envelope (stdout)
`)
}

func cmdKeys() {
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	out := map[string]string{}
	for chain, k := range cryptoenvelope.TrustedFixtures() {
		out[chain] = "0x" + hex.EncodeToString(k.Public)
	}
	if err := enc.Encode(out); err != nil {
		fatal(err)
	}
}

func cmdBlockHash(args []string) {
	fs := flag.NewFlagSet("blockhash", flag.ExitOnError)
	chain := fs.String("chain", "", "source chain (chainA|chainB)")
	height := fs.Int64("height", 0, "block height (>=1)")
	parent := fs.String("parent", "", "parent block hash (0x..) or 0x0 for genesis")
	extras := fs.String("extras", "", "optional salt to simulate a competing block at the same height")
	_ = fs.Parse(args)
	if *chain == "" || *height < 1 || *parent == "" {
		fatal("--chain, --height>=1 and --parent are required")
	}
	fmt.Println(cryptoenvelope.SimBlockHash(*chain, *height, *parent, *extras))
}

func cmdSign(args []string) {
	fs := flag.NewFlagSet("sign", flag.ExitOnError)
	chain := fs.String("chain", "", "source chain (chainA|chainB)")
	channel := fs.String("channel", "", "channel id")
	seq := fs.Uint64("seq", 0, "message sequence")
	block := fs.String("block", "", "anchor block hash (0x..)")
	body := fs.String("body", "", "message body JSON")
	_ = fs.Parse(args)
	if *chain == "" || *channel == "" || *block == "" || *body == "" {
		fatal("--chain, --channel, --seq, --block and --body are required")
	}
	if !json.Valid([]byte(*body)) {
		fatal("--body must be valid JSON")
	}
	key := cryptoenvelope.MustFixtureKey(*chain)
	env, err := cryptoenvelope.Sign(key.Private, *chain, *channel, *block, *seq, json.RawMessage(*body))
	if err != nil {
		fatal(err)
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(env); err != nil {
		fatal(err)
	}
}

func fatal(v any) {
	fmt.Fprintf(os.Stderr, "signfixture: %v\n", v)
	os.Exit(1)
}
