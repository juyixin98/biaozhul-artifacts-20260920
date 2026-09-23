package cryptoenvelope

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
)

// SimBlockHash derives a deterministic simulated source-chain block hash from
// the block's identity: SHA-256 over a fixed preimage. Pass a non-empty extras
// value to simulate a COMPETING block proposal at the same height (a reorg).
// Both the local signing fixture and the tests use this, so hashes agree.
func SimBlockHash(chain string, height int64, parentHash, extras string) string {
	sum := sha256.Sum256([]byte(fmt.Sprintf(
		"ximbox-sim-block/v1:%s:%d:%s:%s", chain, height, parentHash, extras)))
	return "0x" + hex.EncodeToString(sum[:])
}

// ZeroHash is the conventional all-zero parent hash of a genesis block.
const ZeroHash = "0x0000000000000000000000000000000000000000000000000000000000000000"
