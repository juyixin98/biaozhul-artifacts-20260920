// Package node is the client side: it acquires leases, renews them on a
// heartbeat, tracks a conservative local deadline, and writes to the
// protected resource.
//
// The interesting failure mode lives here: after a node is paused longer than
// its lease (see sim.Engine.Pause), the lock service has handed the resource
// to someone else, but on resume the frozen node still holds its old fence
// token in memory and its work loop keeps going. Its writes carry the stale
// token; only the resource's high-water-mark check stops them.
package node

import (
	"fmt"
	"strconv"

	"fencinglease/internal/proto"
	"fencinglease/internal/sim"
)

// Timer/command names used when runner schedules work for a node.
const (
	CmdAcquire = "cmd.acquire"
	CmdSubmit  = "cmd.submit"
	CmdRelease = "cmd.release"
	CmdCompete = "cmd.compete"
	hbTimer    = "heartbeat:"
	deadTimer  = "deadline:"
)

// Server addresses are fixed ids, registered by the runner.
const (
	LockServer = "lockserver"
)

type hold struct {
	fence  int64
	ttl    int64
	expiry int64 // local view of the absolute expiry
	lost   bool  // node knows the lease is gone
	hbSeq  int   // renew request counter
}

// Node is a simulated client.
type Node struct {
	id      string
	resAddr func(resource string) string // map resource name -> resource node id
	holds   map[string]*hold
	pending map[string]int64 // ttl of the in-flight acquire per resource
	reqSeq  int
}

// New creates a client. resAddr resolves a resource name to its node id.
func New(id string, resAddr func(string) string) *Node {
	return &Node{id: id, resAddr: resAddr, holds: map[string]*hold{}, pending: map[string]int64{}}
}

// NodeID implements sim.Handler.
func (n *Node) NodeID() string { return n.id }

// HasFence reports the fence token the node currently associates with res.
func (n *Node) HasFence(res string) int64 {
	if h := n.holds[res]; h != nil {
		return h.fence
	}
	return 0
}

// BelievesHeld reports whether the node still considers itself the holder.
func (n *Node) BelievesHeld(res string) bool {
	h := n.holds[res]
	return h != nil && !h.lost
}

func (n *Node) nextReqID(kind string) string {
	n.reqSeq++
	return fmt.Sprintf("%s#%s-%d", n.id, kind, n.reqSeq)
}

func dataString(d map[string]any, k string) string {
	if v, ok := d[k]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

func dataInt64(d map[string]any, k string) int64 {
	switch v := d[k].(type) {
	case int64:
		return v
	case int:
		return int64(v)
	case float64:
		return int64(v)
	case string:
		x, _ := strconv.ParseInt(v, 10, 64)
		return x
	}
	return 0
}

// Handle processes commands, timers and RPC responses.
func (n *Node) Handle(env *sim.Env, ev *sim.Event) {
	switch ev.Kind {
	case "timer":
		n.handleTimer(env, ev)
	case "message":
		n.handleMessage(env, ev)
	}
}

func (n *Node) handleTimer(env *sim.Env, ev *sim.Event) {
	d := ev.Data
	switch {
	case ev.Name == CmdAcquire:
		n.acquire(env, dataString(d, "resource"), dataInt64(d, "ttl"))
	case ev.Name == CmdSubmit:
		n.submit(env, dataString(d, "resource"), dataString(d, "value"))
	case ev.Name == CmdRelease:
		n.release(env, dataString(d, "resource"))
	case ev.Name == CmdCompete:
		n.competeTick(env, d)
	case len(ev.Name) > len(hbTimer) && ev.Name[:len(hbTimer)] == hbTimer:
		n.heartbeat(env, ev.Name[len(hbTimer):])
	case len(ev.Name) > len(deadTimer) && ev.Name[:len(deadTimer)] == deadTimer:
		n.localExpiry(env, ev.Name[len(deadTimer):])
	}
}

func (n *Node) acquire(env *sim.Env, res string, ttl int64) {
	n.pending[res] = ttl
	req := proto.AcquireReq{Resource: res, TTL: ttl, ReqID: n.nextReqID("acq")}
	env.Record("client.acquire_sent", map[string]any{"resource": res, "ttl": ttl})
	env.Send(&sim.Message{From: n.id, To: LockServer, Method: proto.Acquire, Body: proto.Encode(req)})
}

func (n *Node) startLeaseTimers(env *sim.Env, res string, h *hold) {
	interval := h.ttl / 2
	if interval < 1 {
		interval = 1
	}
	env.After(interval, hbTimer+res, map[string]any{"interval": interval})
	env.At(h.expiry, deadTimer+res, nil)
}

// markLost stops renewal activity. The stale fence token is deliberately kept
// in the hold so that a zombie work loop can still attempt writes with it.
func (n *Node) markLost(env *sim.Env, res string, reason string) {
	h := n.holds[res]
	if h == nil || h.lost {
		return
	}
	h.lost = true
	env.CancelTimers(hbTimer + res)
	env.CancelTimers(deadTimer + res)
	env.Record("client.lock_lost", map[string]any{"resource": res, "fence": h.fence, "reason": reason})
}

func (n *Node) heartbeat(env *sim.Env, res string) {
	h := n.holds[res]
	if h == nil || h.lost {
		return
	}
	h.hbSeq++
	req := proto.RenewReq{Resource: res, Fence: h.fence, TTL: h.ttl, ReqID: n.nextReqID("ren")}
	env.Record("client.renew_sent", map[string]any{"resource": res, "fence": h.fence, "attempt": h.hbSeq})
	env.Send(&sim.Message{From: n.id, To: LockServer, Method: proto.Renew, Body: proto.Encode(req)})
	// Schedule the next heartbeat independently of the reply: a dropped
	// response must not stop renewal. The interval is ttl/2, so one lost
	// renewal attempt is survived; several consecutive losses are not.
	interval := h.ttl / 2
	if interval < 1 {
		interval = 1
	}
	env.After(interval, hbTimer+res, map[string]any{"interval": interval})
}

func (n *Node) localExpiry(env *sim.Env, res string) {
	n.markLost(env, res, "local_deadline_reached")
}

func (n *Node) submit(env *sim.Env, res, value string) {
	h := n.holds[res]
	if h == nil {
		env.Record("client.submit_skipped", map[string]any{"resource": res, "value": value, "reason": "never_held"})
		return
	}
	if h.lost {
		env.Record("client.submit_stale", map[string]any{
			"resource": res, "fence": h.fence, "value": value, "reason": "node_continues_after_loss",
		})
	} else {
		env.Record("client.submit_sent", map[string]any{"resource": res, "fence": h.fence, "value": value})
	}
	req := proto.SubmitReq{Resource: res, Fence: h.fence, Value: value, ReqID: n.nextReqID("sub")}
	env.Send(&sim.Message{From: n.id, To: n.resAddr(res), Method: proto.Submit, Body: proto.Encode(req)})
}

func (n *Node) release(env *sim.Env, res string) {
	h := n.holds[res]
	if h == nil {
		return
	}
	req := proto.ReleaseReq{Resource: res, Fence: h.fence, ReqID: n.nextReqID("rel")}
	env.CancelTimers(hbTimer + res)
	env.CancelTimers(deadTimer + res)
	delete(n.holds, res)
	env.Record("client.release_sent", map[string]any{"resource": res, "fence": h.fence})
	env.Send(&sim.Message{From: n.id, To: LockServer, Method: proto.Release, Body: proto.Encode(req)})
}

// competeTick is one step of a fuzzing work loop. It keeps going for the full
// configured number of steps regardless of lock state: before acquisition it
// waits (there is simply no token to write with), and after losing the lock it
// keeps writing the stale token — a real zombie process does exactly that, and
// it is precisely the workload that exercises the resource's fence defense.
func (n *Node) competeTick(env *sim.Env, d map[string]any) {
	res := dataString(d, "resource")
	step := int(dataInt64(d, "step"))
	count := int(dataInt64(d, "count"))
	interval := dataInt64(d, "interval")
	prefix := dataString(d, "prefix")
	if step >= count {
		return
	}
	if h := n.holds[res]; h != nil {
		n.submit(env, res, fmt.Sprintf("%s-%d", prefix, step))
	}
	next := map[string]any{
		"resource": res, "step": int64(step + 1), "count": int64(count),
		"interval": interval, "prefix": prefix,
	}
	env.After(interval, CmdCompete, next)
}

func (n *Node) handleMessage(env *sim.Env, ev *sim.Event) {
	msg := ev.Msg
	switch msg.Method {
	case proto.AcquireResp:
		var r proto.LockResp
		if err := proto.Decode(msg.Body, &r); err != nil {
			panic(err)
		}
		if !r.OK {
			env.Record("client.acquire_denied", map[string]any{"resource": r.Resource, "reason": r.Reason})
			return
		}
		if existing := n.holds[r.Resource]; existing != nil {
			if existing.fence == r.Fence {
				return // duplicate delivery of the same grant
			}
			if existing.fence > r.Fence {
				// Late grant for an old generation: never overwrite newer state.
				env.Record("client.acquire_ignored_stale", map[string]any{"resource": r.Resource, "fence": r.Fence, "current_fence": existing.fence})
				return
			}
			// A new, higher-fence generation replaces stale state.
			env.CancelTimers(hbTimer + r.Resource)
			env.CancelTimers(deadTimer + r.Resource)
		}
		ttl := n.pending[r.Resource]
		delete(n.pending, r.Resource)
		h := &hold{fence: r.Fence, ttl: ttl, expiry: r.Expiry}
		n.holds[r.Resource] = h
		env.Record("client.acquired", map[string]any{"resource": r.Resource, "fence": r.Fence, "expiry": r.Expiry})
		n.startLeaseTimers(env, r.Resource, h)

	case proto.RenewResp:
		var r proto.LockResp
		if err := proto.Decode(msg.Body, &r); err != nil {
			panic(err)
		}
		h := n.holds[r.Resource]
		if h == nil {
			env.Record("client.renew_result_ignored", map[string]any{"resource": r.Resource, "reason": "no_hold"})
			return
		}
		if r.OK {
			h.expiry = r.Expiry
			env.CancelTimers(deadTimer + r.Resource)
			env.At(r.Expiry, deadTimer+r.Resource, nil)
			if h.lost {
				// A late OK for a still-valid token: the server only renews
				// the current holder, so the lease could not have been given
				// away in between. Resume the heartbeat.
				h.lost = false
				interval := h.ttl / 2
				if interval < 1 {
					interval = 1
				}
				env.After(interval, hbTimer+r.Resource, map[string]any{"interval": interval})
				env.Record("client.lock_recovered", map[string]any{"resource": r.Resource, "fence": r.Fence, "expiry": r.Expiry})
			}
			env.Record("client.renewed", map[string]any{"resource": r.Resource, "fence": r.Fence, "expiry": r.Expiry})
		} else {
			n.markLost(env, r.Resource, "renew_denied:"+r.Reason)
		}

	case proto.SubmitReply:
		var r proto.SubmitResp
		if err := proto.Decode(msg.Body, &r); err != nil {
			panic(err)
		}
		env.Record("client.submit_result", map[string]any{
			"accepted": r.Accepted, "fence": r.Fence, "high_water": r.HighWater,
			"value": r.Value, "reason": r.Reason, "attempt": msg.Attempt,
		})

	case proto.ReleaseResp:
		// State already cleared; nothing further to do.
	}
}
