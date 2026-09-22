// Command genlongstream emits a valid 61-block NDJSON stream (genesis + 60)
// to stdout, used by scripts/acceptance.sh crash/restart checks. Every hash is
// the real SHA-256 of the block's canonical preimage.
package main

import (
	"encoding/json"
	"os"

	"forkindexer/internal/model"
)

func main() {
	const addrA = "0x00000000000000000000000000000000000000aa"
	const addrB = "0x00000000000000000000000000000000000000bb"

	g, _, err := model.Normalize(model.Block{
		Height:       0,
		Transactions: []model.Transfer{{From: "", To: addrA, Amount: "1000000"}},
	})
	if err != nil {
		panic(err)
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(g); err != nil {
		panic(err)
	}

	cur := g
	// A funds B twice (3 then 1), only then B spends back 2: B can never
	// overdraft, so the whole chain stays valid.
	for d := int64(1); d <= 60; d++ {
		var txs []model.Transfer
		switch d % 3 {
		case 1:
			txs = []model.Transfer{{From: addrA, To: addrB, Amount: "3"}}
		case 2:
			txs = []model.Transfer{{From: addrA, To: addrB, Amount: "1"}}
		case 0:
			txs = []model.Transfer{{From: addrB, To: addrA, Amount: "2"}}
		}
		b, _, err := model.Normalize(model.Block{ParentHash: cur.Hash, Height: d, Transactions: txs})
		if err != nil {
			panic(err)
		}
		if err := enc.Encode(b); err != nil {
			panic(err)
		}
		cur = b
	}
}
