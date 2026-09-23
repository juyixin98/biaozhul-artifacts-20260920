// Package sim is the deterministic discrete-event simulator that drives
// causal-broadcast nodes over the unreliable in-process network fabric.
package sim

import (
	"causal-broadcast/internal/netlink"
	"causal-broadcast/internal/node"
)

// eventKind enumerates the scheduled events.
type eventKind int

const (
	evBroadcast eventKind = iota // a node broadcasts a new message
	evArrival                    // a packet copy reaches a destination
)

// event is one scheduled simulation event.
type event struct {
	time      float64
	seq       int64 // insertion tie-breaker for deterministic FIFO ties
	kind      eventKind
	sender    int    // broadcast: broadcasting node; arrival: message sender
	dst       int    // arrival only (-1 otherwise)
	body      string // broadcast only
	msg       node.Message
	scheduled float64 // arrival: the originally planned arrival time
	copyIndex int     // arrival: 0 = first copy, >0 = duplicate
}

// TraceEvent is one entry of the run trace (JSON-serializable).
type TraceEvent struct {
	Time        float64        `json:"time"`
	Type        string         `json:"type"`
	Node        string         `json:"node,omitempty"`
	Dst         string         `json:"dst,omitempty"`
	MsgID       string         `json:"msgId,omitempty"`
	Reason      string         `json:"reason,omitempty"`
	BufferSize  int            `json:"bufferSize,omitempty"`
	BufferCap   int            `json:"bufferCap,omitempty"`
	Clock       map[string]int `json:"clock,omitempty"`
	ClockBefore map[string]int `json:"clockBefore,omitempty"`
	Copies      int            `json:"copies,omitempty"`
	ArrivedAt   float64        `json:"arrivedAt,omitempty"`
	ScheduledAt float64        `json:"scheduledAt,omitempty"`
}

// Delivery records one application-level delivery at one node.
type Delivery struct {
	Time  float64        `json:"time"`
	Node  string         `json:"node"`
	MsgID string         `json:"msgId"`
	Clock map[string]int `json:"clock"`
}

// BufferedDTO is one message stuck in a node's buffer at run end.
type BufferedDTO struct {
	MsgID   string         `json:"msgId"`
	Sender  string         `json:"sender"`
	Clock   map[string]int `json:"clock"`
	Missing []MissingDTO   `json:"missing"`
}

// MissingDTO explains one unsatisfied predecessor.
type MissingDTO struct {
	From string `json:"from"`
	Have int    `json:"have"`
	Need int    `json:"need"`
}

// NodeFinal is a node's state at run end.
type NodeFinal struct {
	Name            string         `json:"name"`
	Clock           map[string]int `json:"clock"`
	DeliveredCount  int            `json:"deliveredCount"`
	Buffered        []BufferedDTO  `json:"buffered"`
	BufferHighWater int            `json:"bufferHighWater"`
}

// Request is the simulator-native run request (decoupled from JSON shape).
type Request struct {
	Names      []string
	BufferCap  int
	NodeBuffer map[string]int

	DefaultLink netlink.Link
	Links       map[[2]int]netlink.Link
	Seed        int64

	Broadcasts []BroadcastSpec
	DropIDs    map[string][]int
	HoldIDs    map[string]map[int][]float64
	DelayedIDs map[string]map[int][]float64

	MaxTime float64 // <= 0 means run until every event drains
}

// BroadcastSpec schedules one broadcast.
type BroadcastSpec struct {
	Time   float64
	Sender int
	Body   string
}
