package eval

import (
	"time"

	"github.com/example/rollout/internal/store"
)

// toStore converts an evaluated observation into its persistence shape.
func toStore(o Observation) store.Observation {
	return store.Observation{
		ReleaseID:   o.ReleaseID,
		Stage:       o.Stage,
		Generation:  o.Generation,
		WindowStart: o.WindowStart.UTC().Format(time.RFC3339Nano),
		WindowEnd:   o.WindowEnd.UTC().Format(time.RFC3339Nano),
		Superseded:  o.Superseded,
		Verdict:     o.Verdict,
	}
}

// fromStore converts a persisted observation back for API responses.
func fromStore(o store.Observation) Observation {
	from, _ := time.Parse(time.RFC3339Nano, o.WindowStart)
	to, _ := time.Parse(time.RFC3339Nano, o.WindowEnd)
	return Observation{
		ReleaseID:   o.ReleaseID,
		Stage:       o.Stage,
		Generation:  o.Generation,
		WindowStart: from,
		WindowEnd:   to,
		Verdict:     o.Verdict,
		Superseded:  o.Superseded,
	}
}

// storeObs is the conversion helper used by the service.
func storeObs(o Observation) store.Observation { return toStore(o) }
