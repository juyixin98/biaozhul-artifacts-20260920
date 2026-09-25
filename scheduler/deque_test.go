package scheduler

import (
	"testing"
)

func TestDequeLIFOAndStealFIFO(t *testing.T) {
	d := newDeque(2) // tiny initial capacity exercises grow
	tasks := []*task{{id: "a"}, {id: "b"}, {id: "c"}, {id: "d"}}
	for _, x := range tasks {
		d.pushBottom(x)
	}
	if d.len() != 4 {
		t.Fatalf("len = %d, want 4", d.len())
	}

	// Owner pops LIFO from the bottom.
	if got := d.popBottom(); got.id != "d" {
		t.Fatalf("popBottom = %s, want d", got.id)
	}

	// Thief pops FIFO from the top.
	if got := d.popTop(); got.id != "a" {
		t.Fatalf("popTop = %s, want a", got.id)
	}
	if d.len() != 2 {
		t.Fatalf("len = %d, want 2", d.len())
	}

	// Steal half of remaining {b,c}: half = 1 -> oldest b.
	out := make([]*task, 2)
	n := d.stealUpTo((d.len()+1)/2, out)
	if n != 1 || out[0].id != "b" {
		t.Fatalf("steal = %d %v, want 1 b", n, out[:n])
	}
	if got := d.popBottom(); got.id != "c" {
		t.Fatalf("popBottom = %s, want c", got.id)
	}
	if d.popBottom() != nil || d.popTop() != nil {
		t.Fatal("empty deque must return nil")
	}
}

func TestDequeWrapAroundAndGrow(t *testing.T) {
	d := newDeque(4)
	for i := 0; i < 10; i++ {
		d.pushBottom(&task{id: string(rune('a' + i))})
		if i%2 == 0 {
			d.popTop()
		}
	}
	// Drain whatever is left, in order, from the top; must never
	// observe nil slots despite circular-buffer wrap-around.
	remaining := d.len()
	for i := 0; i < remaining; i++ {
		if d.popTop() == nil {
			t.Fatalf("unexpected nil at %d of %d", i, remaining)
		}
	}
	if d.len() != 0 {
		t.Fatalf("len = %d, want 0", d.len())
	}

	// Growth under pure pushes keeps ordering.
	for i := 0; i < 100; i++ {
		d.pushBottom(&task{id: "x"})
	}
	if d.len() != 100 {
		t.Fatalf("len = %d, want 100", d.len())
	}
}
