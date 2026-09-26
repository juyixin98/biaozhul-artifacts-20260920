package clock

import (
	"testing"
	"time"
)

func TestFakeMovesForwardOnly(t *testing.T) {
	c := NewFake()
	start := c.Now()
	c.Advance(time.Second)
	c.Advance(2 * time.Second)
	if got := c.Now().Sub(start); got != 3*time.Second {
		t.Fatalf("elapsed = %v, want 3s", got)
	}
}

func TestFakePersistsAndReloads(t *testing.T) {
	dir := t.TempDir()
	c, err := LoadOrCreateFake(dir)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	advanced := c.Advance(7 * time.Second)

	c2, err := LoadOrCreateFake(dir)
	if err != nil {
		t.Fatalf("reload: %v", err)
	}
	if !c2.Now().Equal(advanced) {
		t.Fatalf("reloaded now=%v, want %v (clock must not rewind across restart)", c2.Now(), advanced)
	}
}
