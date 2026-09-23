package service

import (
	"sync"
	"time"

	"vci/internal/domain"
)

// cacheKey identifies a verification answer.
//
//   - Historical queries (asOf non-zero) are keyed by their absolute query
//     instant: the same question against the same snapshot always has the
//     same answer.
//   - "Current" queries use the zero sentinel; the answer stays usable only
//     while (a) the head snapshot is unchanged — any revoke/append moves it
//     — and (b) the service clock has not crossed the credential's validity
//     boundary or a revocation's effective instant (freshUntil).
type cacheKey struct {
	credential string
	purpose    string
	asOf       time.Time // zero = "current" query
	snapshot   int64
}

type cacheEntry struct {
	res        VerifyResult
	stamp      time.Time // clock time at which the entry was computed
	freshUntil time.Time // current queries: earliest verdict boundary; zero = never expires by time alone
}

const defaultCacheTTL = 60 * time.Second

type snapshotCache struct {
	mu  sync.RWMutex
	ttl time.Duration
	now func() time.Time
	m   map[cacheKey]cacheEntry
}

func newSnapshotCache(ttl time.Duration, now func() time.Time) *snapshotCache {
	if ttl <= 0 {
		ttl = defaultCacheTTL
	}
	if now == nil {
		now = func() time.Time { return time.Now().UTC() }
	}
	return &snapshotCache{ttl: ttl, now: now, m: make(map[cacheKey]cacheEntry)}
}

// get returns a cached entry. For "current" queries, now must still be
// before the entry's freshness boundary (and within the TTL).
func (c *snapshotCache) get(k cacheKey, now time.Time) (VerifyResult, bool) {
	c.mu.RLock()
	e, ok := c.m[k]
	c.mu.RUnlock()
	if !ok {
		return VerifyResult{}, false
	}
	if now.Sub(e.stamp) > c.ttl {
		c.mu.Lock()
		delete(c.m, k)
		c.mu.Unlock()
		return VerifyResult{}, false
	}
	if k.asOf.IsZero() && !e.freshUntil.IsZero() && !now.Before(e.freshUntil) {
		// Clock time reached the expiry instant or a revocation became
		// effective since the entry was computed.
		c.mu.Lock()
		delete(c.m, k)
		c.mu.Unlock()
		return VerifyResult{}, false
	}
	return e.res, true
}

func (c *snapshotCache) put(k cacheKey, res VerifyResult, freshUntil time.Time) {
	c.mu.Lock()
	if len(c.m) > 4096 {
		now := c.now()
		for kk, vv := range c.m {
			if now.Sub(vv.stamp) > c.ttl {
				delete(c.m, kk)
			}
		}
	}
	c.m[k] = cacheEntry{res: res, stamp: c.now(), freshUntil: freshUntil}
	c.mu.Unlock()
}

func (c *snapshotCache) size() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.m)
}

// freshBoundary computes the earliest future (relative to now) instant at
// which a "current" verdict over this state could change without an append:
// validity start/end and any scheduled revocation effective time. Zero
// means "no time-driven change pending".
func freshBoundary(st domain.VerificationState, now time.Time) time.Time {
	var b time.Time
	consider := func(t time.Time) {
		if t.After(now) && (b.IsZero() || t.Before(b)) {
			b = t
		}
	}
	if st.Credential != nil {
		consider(st.Credential.NotBefore)
		consider(st.Credential.ExpiresAt)
	}
	if st.Revocation != nil {
		consider(st.Revocation.EffectiveAt)
	}
	return b
}
