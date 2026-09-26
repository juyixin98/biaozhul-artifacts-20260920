// Package cache implements a shared in-process cache whose keys always
// embed the isolation domain and tenant ID, so identical user keys from
// different tenants never collide.
package cache

import "sync"

// IsolationDomain scopes every cache entry to this deployment's
// isolation boundary; it is a fixed part of every composite key.
const IsolationDomain = "tenantiso-v1"

// Cache is a concurrency-safe shared cache with tenant-scoped keys.
type Cache struct {
	mu      sync.RWMutex
	entries map[string][]byte
}

// New returns an empty shared cache.
func New() *Cache { return &Cache{entries: make(map[string][]byte)} }

// compositeKey builds the physical key: domain | tenant | user key.
// The tenant segment is length-prefixed so no tenant/key pair can
// forge another tenant's composite key.
func compositeKey(tenantID, userKey string) string {
	return IsolationDomain + "|" + itoa(len(tenantID)) + ":" + tenantID + "|" + userKey
}

// Get returns the value stored for (tenantID, userKey).
func (c *Cache) Get(tenantID, userKey string) ([]byte, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	v, ok := c.entries[compositeKey(tenantID, userKey)]
	if !ok {
		return nil, false
	}
	out := make([]byte, len(v))
	copy(out, v)
	return out, true
}

// Set stores value under (tenantID, userKey).
func (c *Cache) Set(tenantID, userKey string, value []byte) {
	c.mu.Lock()
	defer c.mu.Unlock()
	v := make([]byte, len(value))
	copy(v, value)
	c.entries[compositeKey(tenantID, userKey)] = v
}

// Len returns the number of entries (test/diagnostic use).
func (c *Cache) Len() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.entries)
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}
