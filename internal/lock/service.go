// Package lock implements the fencing lease lock service.
//
// Every successful acquisition of a resource produces a strictly increasing
// fence token for that resource. A lease expires at an absolute simulated
// time; renewal extends it only if the presented token still identifies the
// current holder. All mutating requests are idempotent under a client supplied
// ReqID, so a retried (duplicated) request after a lost reply cannot mint a
// second fence or corrupt the lease.
package lock

import (
	"sync"

	"fencinglease/internal/proto"
	"fencinglease/internal/sim"
)

// lease is the per-resource lock state.
type lease struct {
	fence    int64  // current fence token; 0 means free
	holder   string // node id of current holder
	expiry   int64  // absolute simulated time
	ttl      int64  // last requested ttl
	released bool
}

type cacheKey struct{ method, reqID string }

// Service is the lock server, one simulated node ("lockserver").
type Service struct {
	id     string
	mu     sync.Mutex // deterministic single goroutine, kept for documentation
	leases map[string]*lease
	fences map[string]int64 // highest token ever handed out per resource
	cache  map[cacheKey]proto.LockResp
}

// New creates a lock service node.
func New(id string) *Service {
	return &Service{id: id, leases: map[string]*lease{}, fences: map[string]int64{}, cache: map[cacheKey]proto.LockResp{}}
}

// NodeID implements sim.Handler.
func (s *Service) NodeID() string { return s.id }

// expiredLocked reports whether l is free at time now, performing lazy expiry.
func (s *Service) expiredLocked(l *lease, now int64) bool {
	if l.fence == 0 {
		return true
	}
	if now >= l.expiry {
		l.fence, l.holder, l.released = 0, "", false
		return true
	}
	return false
}

func (s *Service) reply(env *sim.Env, to string, resp proto.LockResp, method string) {
	env.Send(&sim.Message{From: s.id, To: to, Method: method, Body: proto.Encode(resp)})
}

// Handle dispatches lock RPCs.
func (s *Service) Handle(env *sim.Env, ev *sim.Event) {
	if ev.Kind != "message" {
		return
	}
	msg := ev.Msg
	now := env.Now()
	s.mu.Lock()
	defer s.mu.Unlock()

	switch msg.Method {
	case proto.Acquire:
		var req proto.AcquireReq
		if err := proto.Decode(msg.Body, &req); err != nil {
			panic(err)
		}
		key := cacheKey{proto.Acquire, req.ReqID}
		if cached, ok := s.cache[key]; ok {
			env.Record("lock.served_from_cache", map[string]any{"req_id": req.ReqID, "method": proto.Acquire})
			s.reply(env, msg.From, cached, proto.AcquireResp)
			return
		}
		l := s.leases[req.Resource]
		if l == nil {
			l = &lease{}
			s.leases[req.Resource] = l
		}
		// Re-entrant: same holder while still owned -> return same token.
		if l.fence != 0 && l.holder == msg.From && !s.expiredLocked(l, now) {
			resp := proto.LockResp{OK: true, Fence: l.fence, Resource: req.Resource, Expiry: l.expiry, ReqID: req.ReqID}
			s.cache[key] = resp
			s.reply(env, msg.From, resp, proto.AcquireResp)
			return
		}
		if !s.expiredLocked(l, now) {
			resp := proto.LockResp{OK: false, Resource: req.Resource, Reason: "conflict", Expiry: l.expiry, ReqID: req.ReqID}
			s.cache[key] = resp
			env.Record("lock.acquire_rejected", map[string]any{
				"resource": req.Resource, "holder": l.holder, "fence": l.fence,
			})
			s.reply(env, msg.From, resp, proto.AcquireResp)
			return
		}
		s.fences[req.Resource]++
		token := s.fences[req.Resource]
		l.fence, l.holder, l.ttl, l.released = token, msg.From, req.TTL, false
		l.expiry = now + req.TTL
		resp := proto.LockResp{OK: true, Fence: token, Resource: req.Resource, Expiry: l.expiry, ReqID: req.ReqID}
		s.cache[key] = resp
		env.Record("lock.granted", map[string]any{
			"resource": req.Resource, "fence": token, "holder": msg.From, "expiry": l.expiry,
		})
		s.reply(env, msg.From, resp, proto.AcquireResp)

	case proto.Renew:
		var req proto.RenewReq
		if err := proto.Decode(msg.Body, &req); err != nil {
			panic(err)
		}
		key := cacheKey{proto.Renew, req.ReqID}
		if cached, ok := s.cache[key]; ok {
			env.Record("lock.served_from_cache", map[string]any{"req_id": req.ReqID, "method": proto.Renew})
			s.reply(env, msg.From, cached, proto.RenewResp)
			return
		}
		l := s.leases[req.Resource]
		var resp proto.LockResp
		switch {
		case l == nil || s.expiredLocked(l, now):
			resp = proto.LockResp{OK: false, Resource: req.Resource, Fence: req.Fence, Reason: "lease_expired", ReqID: req.ReqID}
			env.Record("lock.renew_rejected", map[string]any{"resource": req.Resource, "fence": req.Fence, "reason": "lease_expired"})
		case l.fence != req.Fence || l.holder != msg.From:
			resp = proto.LockResp{OK: false, Resource: req.Resource, Fence: req.Fence, Reason: "fencing_token_stale", ReqID: req.ReqID}
			env.Record("lock.renew_rejected", map[string]any{
				"resource": req.Resource, "fence": req.Fence, "current_fence": l.fence, "reason": "fencing_token_stale",
			})
		default:
			l.expiry = now + req.TTL
			l.ttl = req.TTL
			resp = proto.LockResp{OK: true, Fence: l.fence, Resource: req.Resource, Expiry: l.expiry, ReqID: req.ReqID}
			env.Record("lock.renewed", map[string]any{
				"resource": req.Resource, "fence": l.fence, "expiry": l.expiry,
			})
		}
		s.cache[key] = resp
		s.reply(env, msg.From, resp, proto.RenewResp)

	case proto.Release:
		var req proto.ReleaseReq
		if err := proto.Decode(msg.Body, &req); err != nil {
			panic(err)
		}
		key := cacheKey{proto.Release, req.ReqID}
		if cached, ok := s.cache[key]; ok {
			s.reply(env, msg.From, cached, proto.ReleaseResp)
			return
		}
		l := s.leases[req.Resource]
		resp := proto.LockResp{OK: false, Resource: req.Resource, Fence: req.Fence, Reason: "not_holder", ReqID: req.ReqID}
		if l != nil && l.fence == req.Fence && l.holder == msg.From {
			env.Record("lock.released", map[string]any{"resource": req.Resource, "fence": req.Fence})
			l.fence, l.holder, l.released = 0, "", true
			resp = proto.LockResp{OK: true, Fence: req.Fence, Resource: req.Resource, Expiry: now, ReqID: req.ReqID}
		}
		s.cache[key] = resp
		s.reply(env, msg.From, resp, proto.ReleaseResp)
	}
}
