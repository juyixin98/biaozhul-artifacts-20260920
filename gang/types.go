// Package gang implements an in-memory atomic gang scheduler.
//
// A gang is a set of tasks that must be scheduled all-or-nothing: either
// every required node is reserved for the gang in one atomic step, or the
// gang keeps waiting. Reservations are two-phase: a POST reserves nodes and
// hands back an optimistic-concurrency version; a later commit turns the
// reservation into running tasks. The version is re-checked at commit time,
// so any node that changed (went offline, etc.) between reserve and commit
// aborts the whole plan — never a partial start.
package gang

import (
	"errors"
)

// Sentinel errors. Callers (notably the HTTP layer) map these to status codes.
var (
	// ErrNotFound is returned for unknown ids.
	ErrNotFound = errors.New("not found")
	// ErrInvalid is returned for malformed input (bad selectors, sizes...).
	ErrInvalid = errors.New("invalid request")
	// ErrConflict is returned for state conflicts (node in use, version
	// mismatch, gang already in another state...).
	ErrConflict = errors.New("conflict")
	// ErrExpired is returned for reservations that have timed out or been
	// invalidated by a topology change.
	ErrExpired = errors.New("reservation expired or invalidated")
)

// Node is one schedulable host. A node has Capacity identical slots; each
// scheduled task consumes one slot. Labels back node_selector and
// anti_affinity.
type Node struct {
	ID       string
	Zone     string
	Labels   map[string]string
	Capacity int
	Online   bool

	// assigned counts slots held by active plans (reserved or running).
	assigned int
	// reservedBy holds the plan ids that hold slots on this node, one entry
	// per slot.
	reservedBy []string
	// runningBy holds gang ids for committed/running slots, one per slot.
	runningBy []string
	// version is the optimistic-concurrency token. It bumps on every change
	// that could affect a plan that picked this node.
	version int64
}

// TaskSpec is one member task of a gang.
type TaskSpec struct {
	Name         string            `json:"name"`
	NodeSelector map[string]string `json:"node_selector,omitempty"`
}

// GangSpec is a submitted gang request.
type GangSpec struct {
	ID           string     `json:"id"`
	MinNodes     int        `json:"min_nodes"`
	Tasks        []TaskSpec `json:"tasks"`
	AntiAffinity string     `json:"anti_affinity,omitempty"` // label key; each node used for >1 task must share the same value
	TTLMillis    int        `json:"ttl_millis,omitempty"`    // reservation time-to-live; <=0 -> default
}

type gangStatus string

const (
	statusWaiting   gangStatus = "waiting"
	statusReserved  gangStatus = "reserved"
	statusRunning   gangStatus = "running"
	statusSucceeded gangStatus = "succeeded"
	statusExpired   gangStatus = "expired"
)

// Gang is the scheduler-side state of a submitted gang.
type Gang struct {
	Spec   GangSpec
	Status gangStatus
	// ActivePlanID is the id of the current reserved/committed plan, if any.
	ActivePlanID string
}

type planStatus string

const (
	planReserved  planStatus = "reserved"
	planCommitted planStatus = "committed"
	planExpired   planStatus = "expired" // ttl ran out
	planAborted   planStatus = "aborted" // topology change invalidated it
	planReleased  planStatus = "released"
)

// Plan is one reservation/commit attempt for a gang.
type Plan struct {
	ID      string
	GangID  string
	Nodes   []string // nodes chosen, one per slot, may repeat when a node hosts several tasks
	Version int64    // optimistic-concurrency token issued at reserve time
	Status  planStatus
	// nodeVersions snapshots the version of every distinct chosen node at
	// reserve time. Commit re-checks them.
	nodeVersions map[string]int64
	expiresAt    int64 // unix millis
	committedAt  int64
}
