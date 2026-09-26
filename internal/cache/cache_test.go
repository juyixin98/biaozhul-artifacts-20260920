package cache_test

import (
	"context"
	"testing"

	"tenantiso/internal/cache"
	"tenantiso/internal/clock"
	"tenantiso/internal/fakestore"
)

func TestScopedKeyContainsIsolationDomain(t *testing.T) {
	c := cache.New(fakestore.New(clock.Real{}), "compute")
	got := c.ScopedKey("tenant-a", "mykey")
	want := "compute|tenant-a|mykey"
	if got != want {
		t.Fatalf("ScopedKey = %q, want %q", got, want)
	}
}

func TestSameLogicalKeyIsolatedAcrossTenants(t *testing.T) {
	ctx := context.Background()
	c := cache.New(fakestore.New(clock.Real{}), "compute")

	if err := c.Put(ctx, "tenant-a", "shared", []byte("value-a")); err != nil {
		t.Fatalf("put a: %v", err)
	}
	if err := c.Put(ctx, "tenant-b", "shared", []byte("value-b")); err != nil {
		t.Fatalf("put b: %v", err)
	}

	valA, ok, err := c.Get(ctx, "tenant-a", "shared")
	if err != nil || !ok || string(valA) != "value-a" {
		t.Fatalf("tenant-a get = %q ok=%v err=%v", valA, ok, err)
	}
	valB, ok, err := c.Get(ctx, "tenant-b", "shared")
	if err != nil || !ok || string(valB) != "value-b" {
		t.Fatalf("tenant-b get = %q ok=%v err=%v", valB, ok, err)
	}

	// A third tenant sees nothing under the same logical key.
	if _, ok, err := c.Get(ctx, "tenant-c", "shared"); err != nil || ok {
		t.Fatalf("tenant-c must not see the key: ok=%v err=%v", ok, err)
	}
}

func TestKeyVisibility(t *testing.T) {
	c := cache.New(fakestore.New(clock.Real{}), "compute")
	if !c.KeyVisibleToTenant("compute|tenant-a|k", "tenant-a") {
		t.Fatal("tenant-a key must be visible to tenant-a")
	}
	if c.KeyVisibleToTenant("compute|tenant-a|k", "tenant-b") {
		t.Fatal("tenant-a key must not be visible to tenant-b")
	}
}
