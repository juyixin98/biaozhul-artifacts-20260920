package sim

import (
	"testing"

	"causal-broadcast/internal/netlink"
)

func TestSingleNodeLocalDelivery(t *testing.T) {
	req := Request{
		Names:       []string{"A"},
		BufferCap:   5,
		DefaultLink: netlink.Link{BaseDelay: 1},
		Broadcasts: []BroadcastSpec{
			{Time: 0, Sender: 0, Body: "x"},
			{Time: 1, Sender: 0, Body: "y"},
		},
	}
	res := New(req).Run()
	if len(res.Deliveries) != 2 {
		t.Fatalf("want 2 local deliveries, got %d", len(res.Deliveries))
	}
	if res.Nodes[0].Clock["A"] != 2 {
		t.Fatalf("clock A = %v, want 2", res.Nodes[0].Clock)
	}
}

func TestMaxTimeCutoffReportsInflight(t *testing.T) {
	req := baseReq([]string{"A", "B"})
	req.DefaultLink = netlink.Link{BaseDelay: 10}
	req.MaxTime = 2 // A1 arrives at t=10, beyond the cutoff
	req.Broadcasts = []BroadcastSpec{{Time: 0, Sender: 0}}
	res := New(req).Run()

	if res.Complete {
		t.Fatal("run should be incomplete at cutoff")
	}
	if res.PendingArrivals != 1 {
		t.Fatalf("want 1 pending arrival, got %d", res.PendingArrivals)
	}
	if len(res.Diagnostics.PermanentMissing) != 0 {
		t.Fatalf("nothing is permanently missing before drain: %+v", res.Diagnostics.PermanentMissing)
	}
	if len(res.Diagnostics.Blocked) != 1 || res.Diagnostics.Blocked[0].State != "in-flight" {
		t.Fatalf("want one in-flight blocked report, got %+v", res.Diagnostics.Blocked)
	}
}

// A same-sender sequence gap caused by a lost middle message blocks later
// messages from that sender; the root cause is the earliest gap.
func TestSameSenderGapRootCause(t *testing.T) {
	req := baseReq([]string{"A", "B"})
	req.DefaultLink = netlink.Link{BaseDelay: 1}
	req.DropIDs = map[string][]int{"A2": {1}}
	req.Broadcasts = []BroadcastSpec{
		{Time: 0, Sender: 0},
		{Time: 1, Sender: 0},
		{Time: 2, Sender: 0},
	}
	res := New(req).Run()

	byMsg := map[string]MissingReport{}
	for _, m := range res.Diagnostics.PermanentMissing {
		byMsg[m.MsgID] = m
	}
	r2, ok2 := byMsg["A2"]
	if !ok2 || r2.State != "never-arrived" {
		t.Fatalf("A2 report wrong: %+v ok=%v", r2, ok2)
	}
	r3, ok3 := byMsg["A3"]
	if !ok3 {
		t.Fatalf("A3 missing report absent: %+v", byMsg)
	}
	if r3.State != "buffered" {
		t.Fatalf("A3 should be buffered, got %s", r3.State)
	}
	found := false
	for _, rc := range r3.RootCauses {
		if rc == "A2@B" {
			found = true
		}
	}
	if !found {
		t.Fatalf("A3 root causes = %v, want A2@B", r3.RootCauses)
	}
}
