// Package hlc implements a Hybrid Logical Clock.
//
// A timestamp is a (wall, logical) pair. Within a single replica the clock
// hands out strictly increasing timestamps. When an entry carrying a higher
// timestamp is received from another replica, the clock is advanced past it,
// so later local writes order after replicated writes. Two timestamps are
// totally ordered first by wall time and then by the logical counter; ties
// between replicas (equal (wall, log) from different replicas) are broken
// deterministically by replica ID at the store layer.
package hlc

import (
	"encoding/json"
	"fmt"
	"sync"
	"time"
)

// Timestamp is an HLC timestamp. The zero value is the smallest timestamp.
type Timestamp struct {
	Wall uint64 `json:"wall"` // wall-clock component, milliseconds since epoch
	Log  uint64 `json:"log"`  // logical component
}

// Compare returns -1, 0 or 1 as t is smaller than, equal to or greater than o.
func (t Timestamp) Compare(o Timestamp) int {
	if t.Wall != o.Wall {
		if t.Wall < o.Wall {
			return -1
		}
		return 1
	}
	if t.Log != o.Log {
		if t.Log < o.Log {
			return -1
		}
		return 1
	}
	return 0
}

// String renders the timestamp as "wall:log".
func (t Timestamp) String() string {
	return fmt.Sprintf("%d:%d", t.Wall, t.Log)
}

// Equal reports whether two timestamps are identical.
func (t Timestamp) Equal(o Timestamp) bool { return t == o }

// MarshalJSON keeps the wire format small and lower-case.
func (t Timestamp) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Wall uint64 `json:"wall"`
		Log  uint64 `json:"log"`
	}{t.Wall, t.Log})
}

// UnmarshalJSON accepts {"wall":..,"log":..}.
func (t *Timestamp) UnmarshalJSON(data []byte) error {
	var v struct {
		Wall uint64 `json:"wall"`
		Log  uint64 `json:"log"`
	}
	if err := json.Unmarshal(data, &v); err != nil {
		return err
	}
	t.Wall, t.Log = v.Wall, v.Log
	return nil
}

// Clock is a mutable hybrid logical clock bound to one replica.
type Clock struct {
	mu       sync.Mutex
	wall     func() uint64
	wallSeen uint64
	log      uint64
}

// NewClock builds a clock. nowFn may be nil for real wall time (milliseconds).
func NewClock(nowFn func() uint64) *Clock {
	if nowFn == nil {
		nowFn = func() uint64 { return uint64(time.Now().UnixMilli()) }
	}
	return &Clock{wall: nowFn}
}

// Tick returns a timestamp strictly greater than every timestamp previously
// returned by Tick or observed via Observe.
func (c *Clock) Tick() Timestamp {
	c.mu.Lock()
	defer c.mu.Unlock()
	w := c.wall()
	if w <= c.wallSeen {
		c.log++
	} else {
		c.wallSeen = w
		c.log = 0
	}
	return Timestamp{Wall: c.wallSeen, Log: c.log}
}

// Observe advances the clock past t if t is ahead, so that the next Tick is
// ordered after a replicated write. It returns the clock's current time.
func (c *Clock) Observe(t Timestamp) Timestamp {
	c.mu.Lock()
	defer c.mu.Unlock()
	switch {
	case t.Wall > c.wallSeen:
		c.wallSeen = t.Wall
		c.log = t.Log
	case t.Wall == c.wallSeen && t.Log >= c.log:
		c.log = t.Log + 1
	}
	return Timestamp{Wall: c.wallSeen, Log: c.log}
}
