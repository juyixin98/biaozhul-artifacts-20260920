package twopc

import (
	"fmt"

	"twopc-sim/internal/engine"
)

// durable traces a successfully fsynced WAL record. These events are the
// evidence used by the harness invariant checks (durable decision before any
// decision message is sent; no conflicting durable decisions).
func durable(eng *engine.Engine, node engine.NodeID, kind, txn string) {
	eng.Trace("durable", string(node), fmt.Sprintf("record=%s txn=%s", kind, txn))
}
