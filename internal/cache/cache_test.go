package cache

import (
	"testing"
	"time"
)

func fakeClock(start time.Time) (Clock, *time.Time) {
	cur := start
	return func() time.Time { return cur }, &cur
}

func TestGetSetAndExpiry(t *testing.T) {
	now, cur := fakeClock(time.Unix(1000, 0))
	c := NewWithClock(now)
	k := Key{Name: "example.com", QType: 1}
	e := Entry{Answers: []Address{{Type: "A", IP: "192.0.2.1", TTL: 10}}}

	c.Set(k, e, 10*time.Second)
	got, ok := c.Get(k)
	if !ok || len(got.Answers) != 1 {
		t.Fatalf("set/get failed: ok=%v entry=%+v", ok, got)
	}

	*cur = cur.Add(9 * time.Second)
	if _, ok := c.Get(k); !ok {
		t.Fatal("entry should still be live at t=9s")
	}
	*cur = cur.Add(1 * time.Second)
	if _, ok := c.Get(k); ok {
		t.Fatal("entry must be expired at t=10s")
	}
	if c.Len() != 0 {
		t.Fatalf("expired entry not evicted, len=%d", c.Len())
	}
}

// TTL 0 boundary: answers may be returned but must never enter the cache.
func TestTTLZeroNotCached(t *testing.T) {
	c := New()
	k := Key{Name: "tql0.example", QType: 1}
	c.Set(k, Entry{Answers: []Address{{IP: "192.0.2.10"}}}, 0)
	if c.Len() != 0 {
		t.Fatalf("TTL 0 entry was cached, len=%d", c.Len())
	}
	if _, ok := c.Get(k); ok {
		t.Fatal("TTL 0 entry must not be retrievable")
	}
}

// Negative TTL must never store anything.
func TestNegativeTTLNotCached(t *testing.T) {
	c := New()
	k := Key{Name: "neg.example", QType: 1}
	c.Set(k, Entry{}, -5*time.Second)
	if c.Len() != 0 {
		t.Fatal("negative TTL was cached")
	}
}

// Maximum uint32 TTL: the lifetime must compute to a positive duration without
// overflowing into a negative/immediately-expired entry.
func TestMaxUint32TTL(t *testing.T) {
	now, cur := fakeClock(time.Unix(1_000_000_000, 0))
	c := NewWithClock(now)
	k := Key{Name: "ttlmax.example", QType: 1}
	const maxTTL = uint32(4294967295)
	c.Set(k, Entry{Answers: []Address{{IP: "192.0.2.31", TTL: maxTTL}}},
		time.Duration(maxTTL)*time.Second)

	if _, ok := c.Get(k); !ok {
		t.Fatal("max-TTL entry must be stored and live")
	}
	// Advance a year: still live.
	*cur = cur.Add(365 * 24 * time.Hour)
	if _, ok := c.Get(k); !ok {
		t.Fatal("max-TTL entry expired after only one year")
	}
	snap := c.Snapshot()
	if len(snap) != 1 || snap[0].TTLRemain < 4294967295-31536000-1 {
		t.Fatalf("remaining TTL wrong: %+v", snap)
	}
}

func TestSnapshotReportsRemainingTTL(t *testing.T) {
	now, cur := fakeClock(time.Unix(2000, 0))
	c := NewWithClock(now)
	c.Set(Key{Name: "a.example", QType: 1}, Entry{Answers: []Address{{IP: "1.1.1.1", TTL: 30}}}, 30*time.Second)
	c.Set(Key{Name: "b.example", QType: 1}, Entry{Answers: []Address{{IP: "2.2.2.2", TTL: 10}}}, 10*time.Second)

	*cur = cur.Add(7 * time.Second)
	snap := c.Snapshot()
	if len(snap) != 2 {
		t.Fatalf("snapshot len=%d", len(snap))
	}
	rem := map[string]uint32{}
	for _, v := range snap {
		rem[v.Name] = v.TTLRemain
	}
	if rem["a.example"] != 23 || rem["b.example"] != 3 {
		t.Fatalf("remaining TTLs wrong: %v", rem)
	}
}

func TestKeysAreLiteral(t *testing.T) {
	// The cache treats keys literally; canonicalization (lowercasing,
	// trailing-dot removal) is the proxy layer's responsibility.
	c := New()
	c.Set(Key{Name: "WWW.Example.COM", QType: 1},
		Entry{Answers: []Address{{IP: "192.0.2.7"}}}, 10*time.Second)
	if _, ok := c.Get(Key{Name: "WWW.Example.COM", QType: 1}); !ok {
		t.Fatal("identical literal key must hit")
	}
	if _, ok := c.Get(Key{Name: "www.example.com", QType: 1}); ok {
		t.Fatal("different literal key must not collide (canonicalization happens upstream of cache)")
	}
}

func TestFlush(t *testing.T) {
	c := New()
	c.Set(Key{Name: "a", QType: 1}, Entry{}, time.Second)
	c.Set(Key{Name: "b", QType: 28}, Entry{}, time.Second)
	if n := c.Flush(); n != 2 {
		t.Fatalf("flush returned %d, want 2", n)
	}
	if c.Len() != 0 {
		t.Fatal("cache not empty after flush")
	}
}
