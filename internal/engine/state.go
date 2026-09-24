// Package engine implements resumable behavior-tree execution semantics on
// top of the PostgreSQL store: one serialized, numbered, transactional tick
// at a time, deduplicated action dispatches with attempt epochs, subtree
// cancellation and timeout deadlines, all resumable after restart.
package engine

import (
	"encoding/json"
	"fmt"

	"resumable-bt/internal/tree"
)

// Detail structs are the JSONB 'detail' payload per node kind. Only fields
// needed to resume semantics on the next tick are persisted.

type compositeDetail struct {
	// Active is the index of the child the composite is currently on.
	// For sequence/fallback: the first non-terminal child. For parallel:
	// not used (parallel tracks children directly by id).
	Active int `json:"active,omitempty"`
}

type timeoutDetail struct {
	DeadlineUnixMS int64 `json:"deadline_unix_ms"`
}

type parallelDetail struct {
	Successes int `json:"successes"`
	Failures  int `json:"failures"`
}

func marshalDetail(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		// All detail types are plain structs; this cannot realistically fail.
		panic(fmt.Sprintf("marshal detail: %v", err))
	}
	return b
}

// nodeRuntime bundles everything the walker needs for one node.
type nodeRuntime struct {
	def    tree.Node
	status tree.Status // persisted status (empty if never ticked)
	detail json.RawMessage
}

// detailFor parses the persisted detail into the given target; absent/empty
// detail leaves the zero value in place.
func (nr nodeRuntime) detailFor(target any) {
	if len(nr.detail) > 0 && string(nr.detail) != "null" {
		_ = json.Unmarshal(nr.detail, target)
	}
}

// detailOrEmpty returns the persisted detail verbatim, or an empty object.
func (nr nodeRuntime) detailOrEmpty() []byte {
	if len(nr.detail) > 0 {
		return nr.detail
	}
	return []byte("{}")
}
