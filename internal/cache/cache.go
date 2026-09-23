// Package cache is the TTL keyed cache of successful DNS answers.
//
// Only usable answers are ever written here: the proxy guarantees that error
// responses (bad ID, truncated, RCODE != 0, unparsable, no usable answers)
// never reach Set. Entries expire lazily on read and via an optional
// background sweeper.
package cache

import (
	"sync"
	"time"
)

// Entry is one cached resolution.
type Entry struct {
	Name    string    `json:"name"`
	QType   uint16    `json:"qtype"`
	Answers []Address `json:"answers"`
	Expires time.Time `json:"-"`
	Cached  time.Time `json:"cached_at"`
}

// Address is one resolved IP record.
type Address struct {
	Type string `json:"type"` // "A" or "AAAA"
	IP   string `json:"ip"`
	TTL  uint32 `json:"ttl"` // original TTL from the upstream RR
}

// Key is the cache key: canonical lowercase name + question type.
type Key struct {
	Name  string
	QType uint16
}

// maxDuration is the largest representable time.Duration.
const maxDuration = time.Duration(1<<63 - 1)

// Clock abstracts time for tests.
type Clock func() time.Time

// Cache is a goroutine-safe TTL cache.
type Cache struct {
	mu    sync.RWMutex
	store map[Key]Entry
	now   Clock
}

// New creates a cache using the real clock.
func New() *Cache {
	return NewWithClock(time.Now)
}

// NewWithClock creates a cache with an injected clock (tests).
func NewWithClock(now Clock) *Cache {
	if now == nil {
		now = time.Now
	}
	return &Cache{store: make(map[Key]Entry), now: now}
}

// Get returns a live entry. Missing or expired entries return ok=false and are
// removed.
func (c *Cache) Get(k Key) (Entry, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.store[k]
	if !ok {
		return Entry{}, false
	}
	if !c.now().Before(e.Expires) {
		delete(c.store, k)
		return Entry{}, false
	}
	return e, true
}

// Set stores e. Callers must never store error responses. Entries whose
// lifetime is non-positive (TTL 0) are refused. A TTL larger than the
// time.Duration range is clamped rather than overflowing into a negative
// duration.
func (c *Cache) Set(k Key, e Entry, ttl time.Duration) {
	if ttl <= 0 {
		return
	}
	// ttl is derived from a uint32 second count and can never realistically
	// overflow time.Duration (~292 years in ns), but guard explicitly.
	if ttl > maxDuration {
		ttl = maxDuration
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.now()
	if e.Cached.IsZero() {
		e.Cached = now
	}
	e.Name = k.Name
	e.QType = k.QType
	e.Expires = now.Add(ttl)
	c.store[k] = e
}

// Snapshot returns all currently live entries with their remaining TTL in
// seconds. Expired entries encountered are removed.
func (c *Cache) Snapshot() []ViewEntry {
	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.now()
	out := make([]ViewEntry, 0, len(c.store))
	for k, e := range c.store {
		if !now.Before(e.Expires) {
			delete(c.store, k)
			continue
		}
		rem := e.Expires.Sub(now)
		secs := uint64(rem / time.Second)
		out = append(out, ViewEntry{
			Name:      k.Name,
			QType:     k.QType,
			Type:      typeName(k.QType),
			Answers:   e.Answers,
			TTLRemain: uint32(secs),
			Cached:    e.Cached,
		})
	}
	return out
}

// ViewEntry is a cache entry with a remaining-TTL field, for the HTTP view.
type ViewEntry struct {
	Name      string    `json:"name"`
	QType     uint16    `json:"qtype"`
	Type      string    `json:"type"`
	Answers   []Address `json:"answers"`
	TTLRemain uint32    `json:"ttl_remaining_seconds"`
	Cached    time.Time `json:"cached_at"`
}

// Flush removes every entry and returns how many were removed.
func (c *Cache) Flush() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	n := len(c.store)
	c.store = make(map[Key]Entry)
	return n
}

// Len returns the number of entries without expiry cleanup.
func (c *Cache) Len() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.store)
}

// Sweep periodically removes expired entries until stop is closed. Intended to
// be run once per process; tests simply don't start it.
func (c *Cache) Sweep(interval time.Duration, stop <-chan struct{}) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-stop:
			return
		case <-t.C:
			c.Snapshot() // Snapshot drops expired entries as a side effect
		}
	}
}

func typeName(t uint16) string {
	switch t {
	case 1:
		return "A"
	case 28:
		return "AAAA"
	default:
		return "?"
	}
}
