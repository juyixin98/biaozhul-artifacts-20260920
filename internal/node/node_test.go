package node

import (
	"testing"

	"causal-broadcast/internal/vec"
)

func mkMsg(origin vec.Key, deps ...vec.Key) *Message {
	return &Message{Origin: origin, Sender: origin.Node, From: "x", Deps: deps}
}

// Out-of-order arrival: m2 depends on m1. m2 arrives first and buffers; m1
// then arrives and must release m2 in order.
func TestBufferThenRelease(t *testing.T) {
	n := New("n3", 16)
	m1 := mkMsg(vec.Key{Node: "n1", Seq: 1})
	m2 := mkMsg(vec.Key{Node: "n1", Seq: 2}, vec.Key{Node: "n1", Seq: 1})

	out, _, _ := n.Handle(10, m2)
	if out != Buffered || n.BufferLen() != 1 {
		t.Fatalf("m2 should buffer, got %v len=%d", out, n.BufferLen())
	}

	out, delivered, _ := n.Handle(11, m1)
	if out != Delivered || len(delivered) != 2 {
		t.Fatalf("m1 should deliver and release m2, got %v n=%d", out, len(delivered))
	}
	if delivered[0].Origin != m1.Origin || delivered[1].Origin != m2.Origin {
		t.Fatalf("release order wrong: %v then %v", delivered[0].Origin, delivered[1].Origin)
	}
	if !n.Clock().Has(vec.Key{Node: "n1", Seq: 2}) {
		t.Fatal("clock should advance to n1:2")
	}
}

// Duplicate copies of a delivered message, and of one already buffered, must
// be suppressed exactly once each.
func TestDuplicatesSuppressed(t *testing.T) {
	n := New("n3", 16)
	m1 := mkMsg(vec.Key{Node: "n1", Seq: 1})
	m2 := mkMsg(vec.Key{Node: "n1", Seq: 2}, vec.Key{Node: "n1", Seq: 1})

	if out, _, _ := n.Handle(0, m2); out != Buffered {
		t.Fatal("first m2 should buffer")
	}
	if out, _, _ := n.Handle(1, m2); out != Duplicate {
		t.Fatalf("duplicate buffered copy, got %v", out)
	}
	if n.BufferLen() != 1 {
		t.Fatal("duplicate must not be stored")
	}

	if out, _, _ := n.Handle(2, m1); out != Delivered {
		t.Fatal("m1 deliver")
	}
	if out, _, _ := n.Handle(3, m1); out != Duplicate {
		t.Fatalf("duplicate delivered copy, got %v", out)
	}
	if out, _, _ := n.Handle(4, m2); out != Duplicate {
		t.Fatalf("late m2 after release must be duplicate, got %v", out)
	}
	if n.DuplicateCount() != 3 {
		t.Fatalf("dup count = %d, want 3", n.DuplicateCount())
	}
	if n.DeliveriesCount() != 2 {
		t.Fatalf("distinct deliveries = %d, want 2", n.DeliveriesCount())
	}
}

// When the bounded buffer is full, a not-ready copy returns Backpressure and
// must NOT mutate state; a later retry after the predecessor arrives delivers.
func TestBackpressureDoesNotBreakOrder(t *testing.T) {
	n := New("n3", 1)
	m1 := mkMsg(vec.Key{Node: "n1", Seq: 1})
	m2 := mkMsg(vec.Key{Node: "n1", Seq: 2}, vec.Key{Node: "n1", Seq: 1})
	m3 := mkMsg(vec.Key{Node: "n1", Seq: 3}, vec.Key{Node: "n1", Seq: 1}, vec.Key{Node: "n1", Seq: 2})

	// m2 fills the single slot.
	if out, _, _ := n.Handle(0, m2); out != Buffered {
		t.Fatal("m2 buffer")
	}
	// m3 not ready and buffer full -> backpressure, rejected, not stored.
	out, _, bp := n.Handle(1, m3)
	if out != Backpressure || bp == nil || n.BufferLen() != 1 {
		t.Fatalf("m3 should hit backpressure without buffering, got %v len=%d", out, n.BufferLen())
	}
	if len(bp.WaitingOn) == 0 {
		t.Fatal("backpressure event should report waiting_on")
	}

	// Predecessor m1 arrives: releases m2; m3 was not buffered so its retry
	// (fresh copy) now also delivers, in order.
	if out, delivered, _ := n.Handle(2, m1); out != Delivered || len(delivered) != 2 {
		t.Fatalf("m1 should deliver + release m2, got %v n=%d", out, len(delivered))
	}
	out, delivered, _ := n.Handle(3, m3)
	if out != Delivered || len(delivered) != 1 || delivered[0].Origin != m3.Origin {
		t.Fatalf("retry m3 should deliver, got %v %v", out, delivered)
	}
}

// Cross-origin causal dependency: n2's message depends on n1's; n1 late.
func TestCrossOriginCausality(t *testing.T) {
	n := New("n3", 16)
	a := mkMsg(vec.Key{Node: "n1", Seq: 1})
	b := mkMsg(vec.Key{Node: "n2", Seq: 1}, vec.Key{Node: "n1", Seq: 1}) // n2 had seen n1:1

	if out, _, _ := n.Handle(0, b); out != Buffered {
		t.Fatal("b must wait for a")
	}
	out, delivered, _ := n.Handle(1, a)
	if out != Delivered || len(delivered) != 2 {
		t.Fatalf("a should release b, got %v n=%d", out, len(delivered))
	}
	if delivered[1].Origin.Node != "n2" {
		t.Fatal("b must be delivered after a")
	}
}
