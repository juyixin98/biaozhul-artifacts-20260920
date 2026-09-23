package sim

import "fmt"

// Client state.
const (
	StateIdle      = "idle"
	StateHasLease  = "has_lease"
	StateAcquiring = "acquiring"
	StateLostLease = "lost_lease"
	StateReleased  = "released"
)

// ClientEventSummary is the client-side lifecycle counters used by reports.
type ClientEventSummary struct {
	ID             string `json:"id"`
	State          string `json:"state"`
	LeaseID        string `json:"lease_id"`
	Fence          int64  `json:"fence"`
	Granted        int64  `json:"granted"`
	Renewed        int64  `json:"renewed"`
	RenewFailures  int64  `json:"renew_failures"`
	AcquireBusy    int64  `json:"acquire_busy"`
	Commits        int64  `json:"commits"`
	Rejected       int64  `json:"rejected"`
	Dups           int64  `json:"deduped"`
	Pauses         int64  `json:"pauses"`
	PausedFor      Time   `json:"paused_ms_total"`
	HeartbeatsSent int64  `json:"heartbeats_sent"`
}

// Client is a lock-using application node. It renews its lease by periodic
// heartbeat, survives message loss (via retries on acquire only) and can be
// paused by the scenario harness to model a long GC / scheduling stall.
type Client struct {
	id       string
	hb       Time
	retryGap Time

	state   string
	leaseID string
	fence   int64
	// lastFence is the highest fence this client was ever granted. It is
	// intentionally retained after lease loss: a zombie holder (paused
	// process that never observed its expiry) still has its old token in
	// memory and will keep presenting it. The resource must reject it.
	lastFence int64
	expire    Time

	// retryUntil is set by an acquire action with retry=true.
	retry bool
	seq   int64

	s ClientEventSummary
}

func NewClient(id string, heartbeat, retryGap Time) *Client {
	c := &Client{id: id, hb: heartbeat, retryGap: retryGap, state: StateIdle}
	c.s.ID = id
	c.s.State = StateIdle
	return c
}

func (c *Client) ID() string { return c.id }

// nextReqID returns a client-unique id embedded in every request; receivers
// use it to de-duplicate network duplicates and retransmissions.
func (c *Client) nextReqID(kind string) string {
	c.seq++
	return fmt.Sprintf("%s:%s:%d", c.id, kind, c.seq)
}

func (c *Client) send(h Host, e *Envelope) {
	h.Send(e)
}

func (c *Client) HandleMessage(h Host, t Time, msg *Envelope) {
	switch msg.Type {
	case TypeAcquireAck:
		c.onGranted(h, t, msg)
	case TypeAcquireBusy:
		c.s.AcquireBusy++
		h.Log("client.acquire.busy", map[string]any{"fence": msg.Fence, "holder_expire": msg.ExpireAt})
		if c.state == StateAcquiring && c.retry {
			h.SetTimer(c.id, TimerAcqRetry, t+c.retryGap)
		} else {
			c.state = StateIdle
		}

	case TypeRenewAck:
		// Fixed-cadence heartbeats do not re-arm on ack (see onHeartbeat).
		c.s.Renewed++
		c.expire = msg.ExpireAt
		h.Log("client.renewed", map[string]any{"fence": msg.Fence, "expire_at": msg.ExpireAt})

	case TypeRenewErr:
		// The lock service told us the lease is gone. A well-behaved client
		// stops acting as the owner. The resource fence still exists for the
		// case where this error never arrives.
		c.s.RenewFailures++
		c.loseLease(h, t, msg.Reason)

	case TypeReleaseAck:
		c.state = StateReleased
		c.fence = 0
		c.leaseID = ""
		h.CancelTimer(c.id, TimerHeartbeat)
		h.Log("client.released", map[string]any{})

	case TypeReleaseErr:
		h.Log("client.release.err", map[string]any{"reason": msg.Reason})
		c.loseLease(h, t, msg.Reason)

	case TypeSubmitAck:
		switch msg.Result {
		case ResultCommitted:
			c.s.Commits++
		case ResultDuplicate:
			c.s.Dups++
		default:
			c.s.Rejected++
			h.Log("client.submit.rejected", map[string]any{"reason": msg.Result, "max_fence": msg.Fence})
		}
	}
	c.snapshotState()
}

func (c *Client) OnTimer(h Host, t Time, name string) {
	switch name {
	case TimerHeartbeat:
		c.onHeartbeat(h, t)
	case TimerAcqRetry:
		if c.state == StateAcquiring && c.retry {
			c.doAcquire(h, t)
		}
	}
}

// onHeartbeat sends a renewal and re-arms the periodic timer. Heartbeats
// run on a fixed schedule measured from the grant, not from ack arrival:
// scheduling from the ack would let link latency drift the send times and
// model the wrong behavior. Multiple renews may briefly be in flight; the
// service rejects a renew once the lease moved on, which the client treats
// as lease loss.
func (c *Client) onHeartbeat(h Host, t Time) {
	if c.state != StateHasLease {
		return
	}
	c.s.HeartbeatsSent++
	c.send(h, &Envelope{
		Src: c.id, Dst: LockID, Type: TypeRenewReq, ReqType: ReqRenew,
		ReqID: c.nextReqID("renew"), LeaseID: c.leaseID, Fence: c.fence,
	})
	// Absolute cadence: next deadline from "now" at the fixed interval,
	// which is itself the scheduled deadline.
	h.SetTimer(c.id, TimerHeartbeat, t+c.hb)
}

// armHeartbeat schedules the next fixed-cadence renewal from a reference
// time (grant time or the instant of an explicit renew).
func (c *Client) armHeartbeat(h Host, from Time) {
	h.SetTimer(c.id, TimerHeartbeat, from+c.hb)
}

func (c *Client) loseLease(h Host, t Time, reason string) {
	if c.state != StateHasLease && c.state != StateAcquiring {
		return
	}
	c.state = StateLostLease
	c.leaseID = ""
	c.fence = 0
	c.expire = 0
	h.CancelTimer(c.id, TimerHeartbeat)
	h.CancelTimer(c.id, TimerAcqRetry)
	c.snapshotState()
	h.Log("client.lost_lease", map[string]any{"reason": reason})
}

func (c *Client) onGranted(h Host, t Time, msg *Envelope) {
	c.s.Granted++
	c.state = StateHasLease
	c.leaseID = msg.LeaseID
	c.fence = msg.Fence
	c.lastFence = msg.Fence
	c.expire = msg.ExpireAt
	c.retry = false
	h.CancelTimer(c.id, TimerAcqRetry)
	c.armHeartbeat(h, t)
	h.Log("client.granted", map[string]any{
		"lease_id": msg.LeaseID, "fence": msg.Fence, "expire_at": msg.ExpireAt,
	})
}

func (c *Client) OnAction(h Host, t Time, a *ActionSpec) {
	switch a.Op {
	case OpAcquire:
		c.retry = a.Retry
		c.state = StateAcquiring
		c.doAcquire(h, t)
	case OpRenew:
		if c.state == StateHasLease {
			c.s.HeartbeatsSent++
			c.send(h, &Envelope{
				Src: c.id, Dst: LockID, Type: TypeRenewReq, ReqType: ReqRenew,
				ReqID: c.nextReqID("renew"), LeaseID: c.leaseID, Fence: c.fence,
			})
			// An explicit renew re-anchors the heartbeat cadence.
			c.armHeartbeat(h, t)
		} else {
			h.Log("client.action.skip", map[string]any{"op": a.Op, "state": c.state})
		}
	case OpSubmit:
		c.doSubmit(h, t, a)
	case OpRelease:
		if c.state == StateHasLease {
			c.send(h, &Envelope{
				Src: c.id, Dst: LockID, Type: TypeReleaseReq, ReqType: ReqRelease,
				ReqID: c.nextReqID("release"), LeaseID: c.leaseID, Fence: c.fence,
			})
			h.CancelTimer(c.id, TimerHeartbeat)
		} else {
			h.Log("client.action.skip", map[string]any{"op": a.Op, "state": c.state})
		}
	case OpPause:
		c.s.Pauses++
		c.s.PausedFor += a.Duration
		h.Log("client.pause.request", map[string]any{"duration": a.Duration, "until": t + a.Duration})
		h.PauseNode(c.id, t+a.Duration)
	case OpResume:
		h.ResumeNode(c.id)
	case OpNote:
		h.Log("note", map[string]any{"value": a.Value})
	}
	c.snapshotState()
}

func (c *Client) doAcquire(h Host, t Time) {
	c.send(h, &Envelope{
		Src: c.id, Dst: LockID, Type: TypeAcquireReq, ReqType: ReqAcquire,
		ReqID: c.nextReqID("acquire"),
	})
}

// doSubmit writes to the protected resource using the fence of the lease the
// client believes it holds. A paused-then-resumed client whose lease expired
// has already forgotten its fence, unless ForceSubmit is set: that models a
// zombie that never learned it was deposed and keeps using the old fence.
// Either way the resource decides.
func (c *Client) doSubmit(h Host, t Time, a *ActionSpec) {
	res := a.Resource
	if res == "" {
		res = ResourceID
	}
	var fence int64
	switch {
	case a.ForceSubmit && c.lastFence != 0:
		// Zombie path: submit even though we know we are no longer the
		// holder, presenting the last token we were granted. This is the
		// write the fencing token exists to block.
		fence = c.lastFence
	case a.ForceSubmit:
		// A node that imagines it holds a lock it was never granted carries
		// no token at all: fence 0, which the resource also rejects.
		fence = 0
	case c.state == StateHasLease:
		fence = c.fence
	default:
		h.Log("client.submit.refused_locally", map[string]any{
			"state": c.state, "value": a.Value,
		})
		return
	}
	reqID := c.nextReqID("submit")
	c.send(h, &Envelope{
		Src: c.id, Dst: res, Type: TypeSubmitReq, ReqType: ReqSubmit,
		ReqID: reqID, Fence: fence, Value: a.Value,
	})
}

// OnPause is invoked by the engine when the freeze begins. The client's
// heartbeat timer and inbound messages are frozen alongside it; nothing runs
// until resume, so no renewal can escape.
func (c *Client) OnPause(h Host, t Time) {}

// OnResume runs the moment the freeze ends. The client compares "now" with
// the lease expiry it remembers: if the pause outlived the lease, it knows
// the lock is gone. Scenarios also script the zombie submit explicitly to
// prove the resource fence independently of this local check.
func (c *Client) OnResume(h Host, t Time) {
	if c.state == StateHasLease && t >= c.expire {
		c.loseLease(h, t, fmt.Sprintf("paused until %d >= lease expire %d", t, c.expire))
	}
	c.snapshotState()
}

func (c *Client) snapshotState() {
	c.s.State = c.state
	c.s.LeaseID = c.leaseID
	c.s.Fence = c.fence
}

// Snapshot returns terminal client counters.
func (c *Client) Snapshot() ClientEventSummary {
	c.snapshotState()
	return c.s
}
