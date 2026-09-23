package dnscache

import (
	"testing"
	"time"

	"dnsproxy/internal/dnsmsg"
)

type fakeClock struct{ t time.Time }

func (c *fakeClock) nowFn() time.Time        { return c.t }
func (c *fakeClock) advance(d time.Duration) { c.t = c.t.Add(d) }

func newTestCache() (*Cache, *fakeClock) {
	clk := &fakeClock{t: time.Unix(1_000_000, 0)}
	return New(clk.nowFn), clk
}

func sampleEntry(ttl uint32) *Entry {
	return &Entry{
		Name:  "example.com.",
		QType: dnsmsg.TypeA,
		RCode: 0,
		TTL:   ttl,
		Answers: []dnsmsg.Record{
			{Name: "example.com.", Type: dnsmsg.TypeA, Class: dnsmsg.ClassIN, TTL: ttl},
		},
	}
}

func TestSetGetAndExpiry(t *testing.T) {
	c, clk := newTestCache()
	key := Key("EXAMPLE.com", dnsmsg.TypeA)

	if got := c.Get(key); got != nil {
		t.Fatalf("empty cache returned %v", got)
	}

	c.Set(key, sampleEntry(10), 10)
	if c.Len() != 1 {
		t.Fatalf("Len = %d", c.Len())
	}

	got := c.Get(key)
	if got == nil || got.TTL != 10 || len(got.Answers) != 1 {
		t.Fatalf("unexpected cached entry: %+v", got)
	}

	// 3 秒后：仍命中，剩余 TTL 重算为 7。
	clk.advance(3 * time.Second)
	got = c.Get(key)
	if got == nil {
		t.Fatal("entry expired too early")
	}
	if got.TTL != 7 || got.Answers[0].TTL != 7 {
		t.Fatalf("remaining TTL = %d, want 7", got.TTL)
	}

	// 9.999 秒后：仍命中（剩余不足 1 秒时报告至少 1）。
	clk.advance(6999 * time.Millisecond)
	got = c.Get(key)
	if got == nil || got.TTL != 1 {
		t.Fatalf("want TTL floored to 1, got %+v", got)
	}

	// 恰好 10 秒：过期，不得命中。
	clk.advance(time.Millisecond)
	if got := c.Get(key); got != nil {
		t.Fatalf("expired entry still returned: %+v", got)
	}
	if c.Len() != 0 {
		t.Fatalf("Len after expiry = %d, want 0", c.Len())
	}
}

// TTL 边界：0 秒结果不得入缓存。
func TestTTLZeroNotCached(t *testing.T) {
	c, _ := newTestCache()
	key := Key("zero.example.com.", dnsmsg.TypeA)
	c.Set(key, sampleEntry(0), 0)
	if c.Len() != 0 {
		t.Fatalf("TTL=0 entry was cached, Len=%d", c.Len())
	}
	if c.Get(key) != nil {
		t.Fatal("TTL=0 entry was cached")
	}
}

// 最大合法 TTL（2^31-1）边界。
func TestTTLMaxBoundary(t *testing.T) {
	c, clk := newTestCache()
	key := Key("long.example.com.", dnsmsg.TypeA)
	c.Set(key, sampleEntry(0x7fffffff), 0x7fffffff)

	clk.advance(time.Second)
	got := c.Get(key)
	if got == nil {
		t.Fatal("max-TTL entry disappeared")
	}
	if got.TTL != 0x7ffffffe {
		t.Fatalf("TTL = %d, want %d", got.TTL, uint32(0x7ffffffe))
	}
}

// 不同名称/类型不串缓存；A 与 AAAA 独立。
func TestCacheKeySeparation(t *testing.T) {
	c, _ := newTestCache()
	c.Set(Key("a.example.", dnsmsg.TypeA), sampleEntry(10), 10)
	c.Set(Key("a.example.", dnsmsg.TypeAAAA), sampleEntry(10), 10)
	c.Set(Key("b.example.", dnsmsg.TypeA), sampleEntry(10), 10)

	if c.Len() != 3 {
		t.Fatalf("Len = %d, want 3", c.Len())
	}
	if c.Get(Key("A.Example.", dnsmsg.TypeAAAA)) == nil {
		t.Fatal("case-insensitive key miss")
	}
}

func TestDeleteAndFlush(t *testing.T) {
	c, _ := newTestCache()
	k1, k2 := Key("a.", dnsmsg.TypeA), Key("b.", dnsmsg.TypeA)
	c.Set(k1, sampleEntry(10), 10)
	c.Set(k2, sampleEntry(10), 10)

	c.Delete(k1)
	if c.Get(k1) != nil || c.Get(k2) == nil {
		t.Fatal("Delete behaved unexpectedly")
	}
	c.Flush()
	if c.Len() != 0 {
		t.Fatalf("Flush left %d entries", c.Len())
	}
}

// Get 返回的记录是副本，调用方修改不应污染缓存。
func TestGetReturnsCopy(t *testing.T) {
	c, _ := newTestCache()
	key := Key("example.com.", dnsmsg.TypeA)
	c.Set(key, sampleEntry(10), 10)

	first := c.Get(key)
	first.Answers[0].TTL = 1
	second := c.Get(key)
	if second.Answers[0].TTL != 10 {
		t.Fatalf("cache entry mutated through Get copy: TTL=%d", second.Answers[0].TTL)
	}
}
