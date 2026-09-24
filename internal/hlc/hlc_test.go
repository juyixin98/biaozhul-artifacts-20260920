package hlc

import "testing"

func TestTickMonotonic(t *testing.T) {
	var t0 uint64 = 1000
	c := NewClock(func() uint64 { return t0 })

	first := c.Tick()
	if first != (Timestamp{Wall: 1000, Log: 0}) {
		t.Fatalf("first tick = %v, want {1000 0}", first)
	}
	// Frozen wall clock: logical counter advances.
	second := c.Tick()
	if second != (Timestamp{Wall: 1000, Log: 1}) {
		t.Fatalf("second tick = %v, want {1000 1}", second)
	}
	// Wall clock jumps: logical resets to 0.
	t0 = 2000
	third := c.Tick()
	if third != (Timestamp{Wall: 2000, Log: 0}) {
		t.Fatalf("third tick = %v, want {2000 0}", third)
	}
	// Wall clock goes backwards (NTP): logical keeps advancing.
	t0 = 500
	fourth := c.Tick()
	if fourth != (Timestamp{Wall: 2000, Log: 1}) {
		t.Fatalf("fourth tick = %v, want {2000 1}", fourth)
	}
}

func TestObserveAdvancesPastForeignTimestamp(t *testing.T) {
	var wall uint64 = 1000
	c := NewClock(func() uint64 { return wall })

	// Observe a future timestamp from another replica.
	c.Observe(Timestamp{Wall: 5000, Log: 7})
	next := c.Tick()
	if next.Compare(Timestamp{Wall: 5000, Log: 7}) <= 0 {
		t.Fatalf("tick after observe = %v, must be strictly after {5000 7}", next)
	}

	// Observe an older timestamp: nothing changes.
	before := c.Tick()
	c.Observe(Timestamp{Wall: 1, Log: 0})
	after := c.Tick()
	if after.Compare(before) <= 0 {
		t.Fatalf("tick ordering broken around stale observe: %v then %v", before, after)
	}
}

func TestCompareTotalOrder(t *testing.T) {
	cases := []struct {
		a, b Timestamp
		want int
	}{
		{Timestamp{1, 0}, Timestamp{1, 0}, 0},
		{Timestamp{1, 0}, Timestamp{2, 0}, -1},
		{Timestamp{2, 0}, Timestamp{1, 0}, 1},
		{Timestamp{1, 1}, Timestamp{1, 2}, -1},
	}
	for _, tc := range cases {
		if got := tc.a.Compare(tc.b); got != tc.want {
			t.Errorf("%v.Compare(%v) = %d, want %d", tc.a, tc.b, got, tc.want)
		}
	}
}
