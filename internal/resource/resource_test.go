package resource_test

import (
	"testing"

	"fencinglease/internal/proto"
	"fencinglease/internal/resource"
	"fencinglease/internal/sim"
)

type client struct {
	id string
}

func (c *client) NodeID() string              { return c.id }
func (c *client) Handle(*sim.Env, *sim.Event) {}

func submit(eng *sim.Engine, from, resNode, res string, fence int64, value, req string) {
	eng.Send(&sim.Message{
		From: from, To: resNode, Method: proto.Submit,
		Body: proto.Encode(proto.SubmitReq{Resource: res, Fence: fence, Value: value, ReqID: req}),
	})
}

func TestFencedResourceRejectsOlderFence(t *testing.T) {
	eng := sim.New(1, sim.NetConfig{})
	r := resource.New("res-1", true)
	eng.Register(r)
	eng.Register(&client{id: "A"})
	eng.Register(&client{id: "B"})

	submit(eng, "A", "res-1", "acct", 1, "a1", "1")
	eng.Run(1)
	submit(eng, "B", "res-1", "acct", 2, "b1", "2")
	eng.Run(2)
	submit(eng, "A", "res-1", "acct", 1, "a-zombie", "3")
	eng.Run(3)

	log := r.Log()
	if len(log) != 2 {
		t.Fatalf("expected 2 accepted commits, got %d", len(log))
	}
	if log[0].Fence != 1 || log[1].Fence != 2 {
		t.Fatalf("accepted fences should be [1 2], got %d %d", log[0].Fence, log[1].Fence)
	}
	if r.HighWater() != 2 {
		t.Fatalf("high water mark should be 2, got %d", r.HighWater())
	}

	// Verify the rejection trace reason.
	var sawOld bool
	for _, rec := range eng.Records() {
		if rec.Type == "resource.rejected" && rec.Fields["reason"] == "fence_too_old" {
			sawOld = true
		}
	}
	if !sawOld {
		t.Fatal("expected a fence_too_old rejection trace")
	}
}

func TestFencedResourceRejectsSameFence(t *testing.T) {
	eng := sim.New(1, sim.NetConfig{})
	r := resource.New("res-1", true)
	eng.Register(r)
	eng.Register(&client{id: "A"})

	submit(eng, "A", "res-1", "acct", 5, "first", "1")
	eng.Run(1)
	submit(eng, "A", "res-1", "acct", 5, "retry-same-fence", "2")
	eng.Run(2)

	if len(r.Log()) != 1 {
		t.Fatalf("same fence must not commit twice, log=%v", r.Log())
	}
}

func TestFencedResourceRejectsZeroFence(t *testing.T) {
	eng := sim.New(1, sim.NetConfig{})
	r := resource.New("res-1", true)
	eng.Register(r)
	eng.Register(&client{id: "A"})
	submit(eng, "A", "res-1", "acct", 0, "x", "1")
	eng.Run(1)
	if len(r.Log()) != 0 {
		t.Fatal("write without fence must be rejected")
	}
}

func TestUnfencedResourceAcceptsStaleToken(t *testing.T) {
	// Control experiment documenting why fencing is necessary: without the
	// high-water check the stale holder's write lands.
	eng := sim.New(1, sim.NetConfig{})
	r := resource.New("res-1", false)
	eng.Register(r)
	eng.Register(&client{id: "A"})
	eng.Register(&client{id: "B"})
	submit(eng, "A", "res-1", "acct", 2, "new", "1")
	eng.Run(1)
	submit(eng, "B", "res-1", "acct", 1, "stale", "2")
	eng.Run(2)
	if len(r.Log()) != 2 {
		t.Fatalf("naive resource accepts both writes, got %d", len(r.Log()))
	}
}
