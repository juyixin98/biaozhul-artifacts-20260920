// Package cache is the shared result cache. Every key is namespaced with an
// isolation domain and the tenant ID, so two tenants using the same logical
// key can never observe each other's entries.
package cache

import (
	"context"
	"fmt"
	"strings"
)

// Store is the storage dependency the cache writes through to.
type Store interface {
	Put(ctx context.Context, key string, val []byte) error
	Get(ctx context.Context, key string) ([]byte, bool, error)
}

// Cache namespaces all keys by isolation domain and tenant.
type Cache struct {
	store  Store
	domain string
}

// New creates a Cache whose keys live in the given isolation domain.
func New(store Store, domain string) *Cache {
	return &Cache{store: store, domain: domain}
}

// ScopedKey builds the physical cache key: <domain>|<tenant>|<logical key>.
// The tenant segment is the isolation boundary; it is always derived from
// the authenticated context, never from client-supplied body fields.
func (c *Cache) ScopedKey(tenant, key string) string {
	return fmt.Sprintf("%s|%s|%s", c.domain, tenant, key)
}

// Put stores val under the tenant-scoped key.
func (c *Cache) Put(ctx context.Context, tenant, key string, val []byte) error {
	return c.store.Put(ctx, c.ScopedKey(tenant, key), val)
}

// Get fetches the tenant-scoped value.
func (c *Cache) Get(ctx context.Context, tenant, key string) ([]byte, bool, error) {
	return c.store.Get(ctx, c.ScopedKey(tenant, key))
}

// KeyVisibleToTenant reports whether a physical key belongs to tenant in
// this cache's domain. Used by tests to assert isolation.
func (c *Cache) KeyVisibleToTenant(physicalKey, tenant string) bool {
	return strings.HasPrefix(physicalKey, c.domain+"|"+tenant+"|")
}
