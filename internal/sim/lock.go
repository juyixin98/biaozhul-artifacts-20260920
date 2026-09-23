package sim

import (
	"strconv"
)

// Fixed node ids. The scenario models one lock and one protected resource;
// every packet addresses them by these ids.
const (
	LockID     = "L"
	ResourceID = "R"
)

// Timer names.
const (
	TimerLockExpire = "lock.expire"
	TimerHeartbeat  = "heartbeat"
	TimerAcqRetry   = "acquire.retry"
)

// Lease is the lock service's live-lease record.
type Lease struct {
	ID        string `json:"id"`
	Holder    string `json:"holder"`
	Fence     int64  `json:"fence"`
	GrantedAt Time   `json:"granted_at"`
	ExpireAt  Time   `json:"expire_at"`
}

// LockSnapshot is the post-run state of the lock service.
type LockSnapshot struct {
	NodeID      string  `json:"node"`
	NextFence   int64   `json:"next_fence"`
	Grants      int64   `json:"grants"`
	Expirations int64   `json:"expirations"`
	Current     *Lease  `json:"current_lease"`
	History     []Lease `json:"history"`
}

// renewResult caches the answer to a renew request so a network-duplicated
// renew cannot extend the lease twice.
type renewResult struct {
	fence    int64
	expireAt Time
}

// LockService is a single-lock lease manager with monotonic fencing tokens.
type LockService struct {
	ttl Time

	current   *Lease
	nextFence int64
	grants    int64
	expires   int64
	history   []Lease

	// Request de-duplication caches, keyed by the client-unique ReqID.
	// A network-duplicated renew must not extend the lease twice, and a
	// retransmitted acquire must not consume a new fence.
	renewCache   map[string]renewResult
	acquireCache map[string]*Envelope
	releaseCache map[string]*Envelope
}

func NewLockService(ttl Time) *LockService {
	return &LockService{
		ttl:          ttl,
		nextFence:    1,
		history:      []Lease{},
		renewCache:   map[string]renewResult{},
		acquireCache: map[string]*Envelope{},
		releaseCache: map[string]*Envelope{},
	}
}

func (l *LockService) ID() string { return LockID }

func (l *LockService) HandleMessage(h Host, t Time, msg *Envelope) {
	l.expireIfDue(h, t)
	switch msg.Type {
	case TypeAcquireReq:
		l.handleAcquire(h, t, msg)
	case TypeRenewReq:
		l.handleRenew(h, t, msg)
	case TypeReleaseReq:
		l.handleRelease(h, t, msg)
	default:
		h.Log("lock.unexpected", map[string]any{"type": msg.Type})
	}
}

func (l *LockService) OnTimer(h Host, t Time, name string) {
	if name != TimerLockExpire {
		return
	}
	l.expireIfDue(h, t)
}

func (l *LockService) OnAction(Host, Time, *ActionSpec) {}
func (l *LockService) OnPause(Host, Time)               {}
func (l *LockService) OnResume(Host, Time)              {}

// expireIfDue lazily expires the current lease, and also handles the
// scheduled expiry timer. Expiry is what lets the lock move to a new holder
// while the old one may still believe it holds the lock.
func (l *LockService) expireIfDue(h Host, t Time) {
	if l.current == nil || t < l.current.ExpireAt {
		return
	}
	dead := l.current
	l.current = nil
	l.expires++
	h.CancelTimer(LockID, TimerLockExpire)
	h.Log("lock.expire", map[string]any{
		"holder": dead.Holder, "lease_id": dead.ID, "fence": dead.Fence,
		"expire_at": dead.ExpireAt, "at": t,
	})
}

func (l *LockService) reply(h Host, t Time, req *Envelope, ack *Envelope) {
	ack.Src = LockID
	ack.Dst = req.Src
	ack.ReqType = req.ReqType
	ack.ReqID = req.ReqID
	ack.SendTime = t
	h.Send(ack)
}

func (l *LockService) handleAcquire(h Host, t Time, msg *Envelope) {
	if cached, ok := l.acquireCache[msg.ReqID]; ok {
		// Retransmitted acquire of an already-granted lease: answer again
		// without consuming a new fence.
		h.Log("lock.acquire.replay", map[string]any{"client": msg.Src, "req_id": msg.ReqID})
		l.reply(h, t, msg, cached)
		return
	}
	if l.current != nil {
		ack := &Envelope{
			Type:   TypeAcquireBusy,
			Result: ResultBusy,
			Reason: "held",
		}
		ack.LeaseID = l.current.ID
		ack.Fence = l.current.Fence
		ack.ExpireAt = l.current.ExpireAt
		h.Log("lock.acquire.busy", map[string]any{
			"client": msg.Src, "holder": l.current.Holder, "fence": l.current.Fence,
		})
		l.reply(h, t, msg, ack)
		return
	}

	lease := &Lease{
		ID:        "lease-" + strconv.FormatInt(l.nextFence, 10),
		Holder:    msg.Src,
		Fence:     l.nextFence,
		GrantedAt: t,
		ExpireAt:  t + l.ttl,
	}
	l.nextFence++
	l.grants++
	l.current = lease
	l.history = append(l.history, *lease)

	ack := &Envelope{
		Type: TypeAcquireAck, Result: ResultGranted,
		LeaseID: lease.ID, Fence: lease.Fence, ExpireAt: lease.ExpireAt,
	}
	l.acquireCache[msg.ReqID] = ack
	h.SetTimer(LockID, TimerLockExpire, lease.ExpireAt)
	h.Log("lock.grant", map[string]any{
		"client": msg.Src, "lease_id": lease.ID, "fence": lease.Fence,
		"expire_at": lease.ExpireAt,
	})
	l.reply(h, t, msg, ack)
}

func (l *LockService) handleRenew(h Host, t Time, msg *Envelope) {
	// Duplicated renew packet: repeat the recorded answer, never re-extend.
	if cached, ok := l.renewCache[msg.ReqID]; ok {
		h.Log("lock.renew.dedup", map[string]any{
			"client": msg.Src, "req_id": msg.ReqID, "fence": cached.fence,
		})
		ack := &Envelope{
			Type: TypeRenewAck, Result: ResultCommitted,
			LeaseID: msg.LeaseID, Fence: cached.fence, ExpireAt: cached.expireAt,
		}
		l.reply(h, t, msg, ack)
		return
	}

	if l.current == nil || l.current.Holder != msg.Src || l.current.ID != msg.LeaseID {
		h.Log("lock.renew.expired", map[string]any{
			"client": msg.Src, "lease_id": msg.LeaseID,
		})
		l.reply(h, t, msg, &Envelope{
			Type: TypeRenewErr, Result: ResultExpired, Reason: "lease expired or unknown",
		})
		return
	}

	newExpire := t + l.ttl
	l.current.ExpireAt = newExpire
	l.renewCache[msg.ReqID] = renewResult{fence: l.current.Fence, expireAt: newExpire}
	// Move the lease's expiry boundary with the new deadline.
	h.SetTimer(LockID, TimerLockExpire, newExpire)
	h.Log("lock.renew", map[string]any{
		"client": msg.Src, "lease_id": l.current.ID, "fence": l.current.Fence,
		"expire_at": newExpire,
	})
	l.reply(h, t, msg, &Envelope{
		Type: TypeRenewAck, Result: ResultCommitted,
		LeaseID: l.current.ID, Fence: l.current.Fence, ExpireAt: newExpire,
	})
}

func (l *LockService) handleRelease(h Host, t Time, msg *Envelope) {
	if cached, ok := l.releaseCache[msg.ReqID]; ok {
		h.Log("lock.release.replay", map[string]any{"client": msg.Src, "req_id": msg.ReqID})
		l.reply(h, t, msg, cached)
		return
	}
	if l.current == nil || l.current.Holder != msg.Src || l.current.ID != msg.LeaseID {
		l.reply(h, t, msg, &Envelope{
			Type: TypeReleaseErr, Result: ResultExpired, Reason: "not the live lease",
		})
		return
	}
	released := l.current
	l.current = nil
	h.CancelTimer(LockID, TimerLockExpire)
	ack := &Envelope{Type: TypeReleaseAck, Result: ResultCommitted, Fence: released.Fence}
	l.releaseCache[msg.ReqID] = ack
	h.Log("lock.release", map[string]any{
		"client": msg.Src, "lease_id": released.ID, "fence": released.Fence,
	})
	l.reply(h, t, msg, ack)
}

// Snapshot returns the terminal state for the run report.
func (l *LockService) Snapshot() LockSnapshot {
	snap := LockSnapshot{
		NodeID:      LockID,
		NextFence:   l.nextFence,
		Grants:      l.grants,
		Expirations: int64(l.expires),
		History:     append([]Lease(nil), l.history...),
	}
	if l.current != nil {
		c := *l.current
		snap.Current = &c
	}
	return snap
}
