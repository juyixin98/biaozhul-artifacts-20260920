package cache_test

import (
	"testing"

	"example.com/tenantiso/internal/cache"
)

func TestCompositeKeyIsolation(t *testing.T) {
	c := cache.New()
	c.Set("tenant-a", "k", []byte("A"))
	c.Set("tenant-b", "k", []byte("B"))

	if v, _ := c.Get("tenant-a", "k"); string(v) != "A" {
		t.Fatalf("tenant-a got %q", v)
	}
	if v, _ := c.Get("tenant-b", "k"); string(v) != "B" {
		t.Fatalf("tenant-b got %q", v)
	}
	if _, ok := c.Get("tenant-c", "k"); ok {
		t.Fatal("tenant-c should miss")
	}
}

func TestCompositeKeyPrefixForgery(t *testing.T) {
	c := cache.New()
	// Crafted pair that would collide without length-prefixing:
	// ("ab", "c|k") vs ("abc", "k") style boundary attacks.
	c.Set("ab", "c", []byte("1"))
	if _, ok := c.Get("abc", ""); ok {
		t.Fatal("boundary forgery produced a collision")
	}
	if _, ok := c.Get("a", "bc"); ok {
		t.Fatal("boundary forgery produced a collision")
	}
}

func TestIsolationDomainInKey(t *testing.T) {
	if cache.IsolationDomain == "" {
		t.Fatal("isolation domain must be non-empty")
	}
}
