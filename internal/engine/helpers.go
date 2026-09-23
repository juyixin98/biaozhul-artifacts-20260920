package engine

import (
	"encoding/json"
	"time"

	"github.com/example/snapshotprune/internal/types"
)

// pinEntry couples a snapshot release function to the lease it protects.
// Release functions are keyed by lease id so lease expiry/removal can drop
// exactly the snapshot pins that lease acquired (normally just one).
type pinEntry struct {
	leaseID string
	base    uint64
	release func()
}

// pinPorts are declared on Engine in fields.go.

func jsonGenesis(g *types.Genesis) ([]byte, error) {
	return json.Marshal(g)
}

func jsonGenesisDecode(b []byte, g *types.Genesis) error {
	return json.Unmarshal(b, g)
}

// ttlLeft is a small helper used by API serialization.
func (e *Engine) ttlLeft(exp time.Time) time.Duration {
	d := exp.Sub(e.clock.Now())
	if d < 0 {
		return 0
	}
	return d
}
