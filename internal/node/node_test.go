package node

import "testing"

func vcOf(n int, pairs ...int) VC {
	v := make(VC, n)
	for i := 0; i+1 < len(pairs); i += 2 {
		v[pairs[i]] = pairs[i+1]
	}
	return v
}

func TestDeliverableRule(t *testing.T) {
	// cur = {A:1,B:0}; A2 requires exactly A=2 and nothing ahead.
	cur := vcOf(2, 0, 1)
	if !Deliverable(cur, vcOf(2, 0, 2), 0) {
		t.Fatal("A2 should be deliverable after A1")
	}
	// A3 is a same-stream gap.
	if Deliverable(cur, vcOf(2, 0, 3), 0) {
		t.Fatal("A3 must not deliver before A2")
	}
	// B1 depending on A=2 is a cross-node gap.
	if Deliverable(cur, vcOf(2, 0, 2, 1, 1), 1) {
		t.Fatal("B1(A=2) must not deliver while A is at 1")
	}
	// Independent B1 is fine.
	if !Deliverable(cur, vcOf(2, 0, 1, 1, 1), 1) {
		t.Fatal("independent B1 should deliver")
	}
}

func TestBufferReleaseCascade(t *testing.T) {
	names := []string{"A", "B", "C"}
	n := New(2, 3, 10, names) // node C

	a1 := Message{ID: "A1", Sender: 0, Seq: 1, Clock: vcOf(3, 0, 1)}
	b1 := Message{ID: "B1", Sender: 1, Seq: 1, Clock: vcOf(3, 0, 1, 1, 1)}
	c1 := Message{ID: "C1", Sender: 2, Seq: 1, Clock: vcOf(3, 0, 1, 1, 1, 2, 1)}

	// Out of order: C1 arrives first (needs A1 and B1), then B1 (needs A1),
	// then A1.
	if r := n.Receive(c1); r.Newly != OutcomeBuffered {
		t.Fatalf("C1: want buffered, got %s", r.Newly)
	}
	if r := n.Receive(b1); r.Newly != OutcomeBuffered {
		t.Fatalf("B1: want buffered, got %s", r.Newly)
	}
	r := n.Receive(a1)
	if r.Newly != OutcomeDelivered {
		t.Fatalf("A1: want delivered, got %s", r.Newly)
	}
	// Cascade must release B1 then C1, in causal order.
	if len(r.Delivered) != 3 {
		t.Fatalf("want 3 deliveries (A1,B1,C1), got %d", len(r.Delivered))
	}
	got := []string{r.Delivered[0].Msg.ID, r.Delivered[1].Msg.ID, r.Delivered[2].Msg.ID}
	want := []string{"A1", "B1", "C1"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("cascade order = %v, want %v", got, want)
		}
	}
	if n.BufferedCount() != 0 {
		t.Fatalf("buffer should be empty, has %d", n.BufferedCount())
	}
	if n.Clock()[2] != 1 || n.Clock()[0] != 1 || n.Clock()[1] != 1 {
		t.Fatalf("final clock wrong: %v", n.Clock())
	}
}

func TestDuplicateDeliveredOnce(t *testing.T) {
	names := []string{"A", "B"}
	n := New(1, 2, 10, names)
	m := Message{ID: "A1", Sender: 0, Seq: 1, Clock: vcOf(2, 0, 1)}

	if r := n.Receive(m); r.Newly != OutcomeDelivered {
		t.Fatalf("first copy: want delivered, got %s", r.Newly)
	}
	if r := n.Receive(m); r.Newly != OutcomeDuplicate {
		t.Fatalf("second copy: want duplicate, got %s", r.Newly)
	}
	if n.DeliveredCount() != 1 {
		t.Fatalf("exactly-once violated: %d deliveries", n.DeliveredCount())
	}

	// A duplicate copy of a message already sitting in the buffer is also
	// suppressed and must not consume a buffer slot.
	a2 := Message{ID: "A2", Sender: 0, Seq: 2, Clock: vcOf(2, 0, 2)}
	n2 := New(1, 2, 1, names)
	if r := n2.Receive(a2); r.Newly != OutcomeBuffered {
		t.Fatalf("A2: want buffered, got %s", r.Newly)
	}
	if r := n2.Receive(a2); r.Newly != OutcomeDuplicate {
		t.Fatalf("buffered duplicate: want duplicate, got %s", r.Newly)
	}
	if n2.BufferedCount() != 1 {
		t.Fatal("duplicate of buffered message must not add a slot")
	}
}

func TestBackpressureLeavesStateUntouched(t *testing.T) {
	names := []string{"A", "B"}
	n := New(1, 2, 1, names) // buffer capacity 1 at node B

	a2 := Message{ID: "A2", Sender: 0, Seq: 2, Clock: vcOf(2, 0, 2)}
	a3 := Message{ID: "A3", Sender: 0, Seq: 3, Clock: vcOf(2, 0, 3)}

	if r := n.Receive(a2); r.Newly != OutcomeBuffered {
		t.Fatalf("A2: want buffered, got %s", r.Newly)
	}
	// Buffer full: A3 is rejected with backpressure and nothing changes.
	r := n.Receive(a3)
	if r.Newly != OutcomeBackpressure {
		t.Fatalf("A3: want backpressure, got %s", r.Newly)
	}
	if n.InBuffer("A3") || n.Delivered("A3") {
		t.Fatal("backpressured message must not be stored")
	}
	// Retrying A3 once A2 is released must succeed and keep causal order.
	a1 := Message{ID: "A1", Sender: 0, Seq: 1, Clock: vcOf(2, 0, 1)}
	dr := n.Receive(a1)
	if dr.Newly != OutcomeDelivered || len(dr.Delivered) != 2 {
		t.Fatalf("A1 should release A1+A2 cascade, got %+v", dr)
	}
	if dr.Delivered[1].Msg.ID != "A2" {
		t.Fatalf("causal order broken: %s", dr.Delivered[1].Msg.ID)
	}
	// Now retry the previously rejected A3.
	dr = n.Receive(a3)
	if dr.Newly != OutcomeDelivered || dr.Delivered[0].Msg.ID != "A3" {
		t.Fatalf("retried A3 should deliver, got %+v", dr)
	}
}

func TestMissingDepsReport(t *testing.T) {
	names := []string{"A", "B", "C"}
	n := New(2, 3, 10, names)
	c1 := Message{ID: "C1", Sender: 2, Seq: 1, Clock: vcOf(3, 0, 1, 1, 1)}
	if r := n.Receive(c1); r.Newly != OutcomeBuffered {
		t.Fatalf("want buffered, got %s", r.Newly)
	}
	missing, ok := n.MissingDepsFor("C1")
	if !ok || len(missing) != 2 {
		t.Fatalf("want 2 missing deps, got %+v ok=%v", missing, ok)
	}
}
