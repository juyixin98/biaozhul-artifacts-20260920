package lock_test

import (
	"testing"

	"fencinglease/internal/lock"
	"fencinglease/internal/proto"
	"fencinglease/internal/sim"
)

// client is a scripted RPC actor: each queued "send" timer transmits one
// prebuilt request to the lock server.
type client struct {
	id    string
	steps []step
}

type step struct {
	at     int64
	method string
	body   any
}

func (c *client) NodeID() string { return c.id }

func (c *client) Handle(env *sim.Env, ev *sim.Event) {
	if ev.Kind != "timer" {
		return
	}
	st := c.steps[ev.Data["idx"].(int64)]
	env.Send(&sim.Message{From: c.id, To: "lockserver", Method: st.method, Body: proto.Encode(st.body)})
}

func (c *client) install(eng *sim.Engine) {
	eng.Register(c)
	for i, st := range c.steps {
		i := i
		eng.Schedule(st.at, c.id, "send", map[string]any{"idx": int64(i)})
	}
}

// sink swallows reply messages.
type sink struct{ id string }

func (sink) NodeID() string              { return "sink" }
func (sink) Handle(*sim.Env, *sim.Event) {}

func setup(t *testing.T) *sim.Engine {
	t.Helper()
	eng := sim.New(1, sim.NetConfig{})
	eng.Register(lock.New("lockserver"))
	eng.Register(sink{})
	return eng
}

func grantedFences(eng *sim.Engine) []int64 {
	var fs []int64
	for _, r := range eng.Records() {
		if r.Type == "lock.granted" {
			fs = append(fs, r.Fields["fence"].(int64))
		}
	}
	return fs
}

func hasRecord(eng *sim.Engine, recType, reason string) bool {
	for _, r := range eng.Records() {
		if r.Type != recType {
			continue
		}
		if reason == "" || r.Fields["reason"] == reason {
			return true
		}
	}
	return false
}

func TestFenceTokensIncreasePerAcquisition(t *testing.T) {
	eng := setup(t)
	(&client{id: "A", steps: []step{
		{0, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 10, ReqID: "a1"}},
		{3, proto.Release, proto.ReleaseReq{Resource: "r1", Fence: 1, ReqID: "ar1"}},
	}}).install(eng)
	(&client{id: "B", steps: []step{
		{5, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 10, ReqID: "b1"}},
	}}).install(eng)
	eng.Run(10)
	fs := grantedFences(eng)
	if len(fs) != 2 || fs[0] != 1 || fs[1] != 2 {
		t.Fatalf("expected fences [1 2], got %v", fs)
	}
}

func TestAcquireConflictWhileHeld(t *testing.T) {
	eng := setup(t)
	(&client{id: "A", steps: []step{
		{0, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 50, ReqID: "a1"}},
	}}).install(eng)
	(&client{id: "B", steps: []step{
		{2, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 50, ReqID: "b1"}},
	}}).install(eng)
	eng.Run(10)
	if fs := grantedFences(eng); len(fs) != 1 || fs[0] != 1 {
		t.Fatalf("B must be rejected while A holds; grants=%v", fs)
	}
}

func TestExpiredLeaseAllowsNewFence(t *testing.T) {
	eng := setup(t)
	(&client{id: "A", steps: []step{
		{0, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 5, ReqID: "a1"}},
	}}).install(eng)
	(&client{id: "B", steps: []step{
		{7, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 5, ReqID: "b1"}},
	}}).install(eng)
	eng.Run(10)
	fs := grantedFences(eng)
	if len(fs) != 2 || fs[1] != 2 {
		t.Fatalf("expected regrant with fence 2 after expiry, got %v", fs)
	}
}

func TestRenewStaleFenceRejected(t *testing.T) {
	eng := setup(t)
	(&client{id: "A", steps: []step{
		{0, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 50, ReqID: "a1"}},
		{2, proto.Release, proto.ReleaseReq{Resource: "r1", Fence: 1, ReqID: "ar"}},
		{6, proto.Renew, proto.RenewReq{Resource: "r1", Fence: 1, TTL: 10, ReqID: "ax"}},
	}}).install(eng)
	(&client{id: "B", steps: []step{
		{4, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 50, ReqID: "b1"}},
	}}).install(eng)
	eng.Run(10)
	if !hasRecord(eng, "lock.renew_rejected", "fencing_token_stale") {
		t.Fatal("renewal with stale fence must be rejected fencing_token_stale")
	}
}

func TestRenewAfterExpiryRejected(t *testing.T) {
	eng := setup(t)
	(&client{id: "A", steps: []step{
		{0, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 5, ReqID: "a1"}},
		{7, proto.Renew, proto.RenewReq{Resource: "r1", Fence: 1, TTL: 10, ReqID: "ax"}},
	}}).install(eng)
	eng.Run(10)
	if !hasRecord(eng, "lock.renew_rejected", "lease_expired") {
		t.Fatal("renewal after lease expiry must be rejected lease_expired")
	}
}

func TestDuplicateAcquireIsIdempotent(t *testing.T) {
	eng := setup(t)
	(&client{id: "A", steps: []step{
		{0, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 50, ReqID: "same"}},
		{1, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 50, ReqID: "same"}},
	}}).install(eng)
	eng.Run(5)
	if fs := grantedFences(eng); len(fs) != 1 || fs[0] != 1 {
		t.Fatalf("duplicated acquire must not mint a second fence, got %v", fs)
	}
	if !hasRecord(eng, "lock.served_from_cache", "") {
		t.Fatal("duplicate request should be served from idempotency cache")
	}
}

func TestRenewExtendsExpiry(t *testing.T) {
	eng := setup(t)
	// ttl=10 (expiry 11); renewed at t=8 -> expiry 18; B at t=15 must lose.
	(&client{id: "A", steps: []step{
		{0, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 10, ReqID: "a1"}},
		{8, proto.Renew, proto.RenewReq{Resource: "r1", Fence: 1, TTL: 10, ReqID: "r1"}},
	}}).install(eng)
	(&client{id: "B", steps: []step{
		{15, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 10, ReqID: "b1"}},
	}}).install(eng)
	eng.Run(20)
	if fs := grantedFences(eng); len(fs) != 1 {
		t.Fatalf("renewed lease should still block B at t=15, grants=%v", fs)
	}
}

func TestReentrantAcquireSameFence(t *testing.T) {
	eng := setup(t)
	(&client{id: "A", steps: []step{
		{0, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 50, ReqID: "a1"}},
		{3, proto.Acquire, proto.AcquireReq{Resource: "r1", TTL: 50, ReqID: "a2"}},
	}}).install(eng)
	eng.Run(6)
	fs := grantedFences(eng)
	if len(fs) != 1 {
		t.Fatalf("re-entrant acquire by same holder must return same fence, grants=%v", fs)
	}
}
