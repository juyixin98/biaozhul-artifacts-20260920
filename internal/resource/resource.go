// Package resource is the storage protected by the lock.
//
// The resource does not talk to the lock service and does not know about
// leases or time. Its only defense is the fence token carried by every write:
// it remembers the largest token it has ever accepted and rejects any write
// presenting a smaller one. That single rule is what makes a paused/stale lock
// holder harmless even if the client believes it still owns the lock.
//
// Fenced controls the policy. Fenced=true (the real system): token must be
// strictly greater than the high-water mark; duplicate writes with the same
// token are also rejected, so each successful write appears exactly once and
// in token order. Fenced=false models the naive baseline used by the
// no-fencing demo, where any non-zero token is accepted.
package resource

import (
	"fencinglease/internal/proto"
	"fencinglease/internal/sim"
)

// Entry is one accepted write.
type Entry struct {
	Time  int64  `json:"time"`
	Fence int64  `json:"fence"`
	Node  string `json:"node"`
	Value string `json:"value"`
}

// Resource is a simulated resource node.
type Resource struct {
	id      string
	fenced  bool
	high    int64
	log     []Entry
	seenReq map[string]bool
}

// New creates a resource. fenced toggles fence-token enforcement.
func New(id string, fenced bool) *Resource {
	return &Resource{id: id, fenced: fenced, seenReq: map[string]bool{}}
}

// NodeID implements sim.Handler.
func (r *Resource) NodeID() string { return r.id }

// HighWater returns the largest accepted fence token.
func (r *Resource) HighWater() int64 { return r.high }

// Log returns the accepted writes in acceptance order.
func (r *Resource) Log() []Entry { out := make([]Entry, len(r.log)); copy(out, r.log); return out }

// Handle processes submit RPCs.
func (r *Resource) Handle(env *sim.Env, ev *sim.Event) {
	if ev.Kind != "message" || ev.Msg.Method != proto.Submit {
		return
	}
	msg := ev.Msg
	var req proto.SubmitReq
	if err := proto.Decode(msg.Body, &req); err != nil {
		panic(err)
	}

	reason := ""
	accept := true
	switch {
	case req.Fence == 0:
		accept, reason = false, "no_fence"
	case !r.fenced:
		// Naive baseline: the holder "has a token", so the write is taken.
		accept = true
	case req.Fence < r.high:
		accept, reason = false, "fence_too_old"
	case req.Fence == r.high:
		accept, reason = false, "duplicate_fence"
	case r.seenReq[req.ReqID]:
		accept, reason = false, "duplicate_request"
	}

	if accept {
		r.high = req.Fence
		r.seenReq[req.ReqID] = true
		r.log = append(r.log, Entry{Time: env.Now(), Fence: req.Fence, Node: msg.From, Value: req.Value})
		env.Record("resource.accepted", map[string]any{
			"resource": req.Resource, "fence": req.Fence, "value": req.Value,
			"high_water": r.high, "node": msg.From,
		})
	} else {
		env.Record("resource.rejected", map[string]any{
			"resource": req.Resource, "fence": req.Fence, "high_water": r.high,
			"value": req.Value, "reason": reason, "node": msg.From,
		})
	}

	resp := proto.SubmitResp{
		OK: accept, Accepted: accept, Fence: req.Fence, HighWater: r.high,
		Reason: reason, Value: req.Value, ReqID: req.ReqID,
	}
	env.Send(&sim.Message{From: r.id, To: msg.From, Method: proto.SubmitReply, Body: proto.Encode(resp)})
}
