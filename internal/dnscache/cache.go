// Package dnscache 提供带 TTL 的 DNS 应答内存缓存。
//
// 条目在到达其 TTL 绝对过期时刻后惰性失效；TTL 为 0 的结果不会被写入。
// 命中时返回的记录 TTL 会按已过时间重算，不会向上游确认续期。
package dnscache

import (
	"sync"
	"time"

	"dnsproxy/internal/dnsmsg"
)

// Entry 是一条缓存结果。TTL 为响应时的原始 TTL。
type Entry struct {
	Name      string
	QType     uint16
	Answers   []dnsmsg.Record
	RCode     byte
	TTL       uint32
	storedAt  time.Time
	expiresAt time.Time
}

// Cache 是并发安全的 TTL 缓存。零值不可用，请使用 New。
type Cache struct {
	mu    sync.RWMutex
	items map[string]*Entry
	now   func() time.Time
}

// New 创建缓存；now 可为 nil（使用 time.Now）。
func New(now func() time.Time) *Cache {
	if now == nil {
		now = time.Now
	}
	return &Cache{
		items: make(map[string]*Entry),
		now:   now,
	}
}

// Key 生成缓存键：小写规范名 + 查询类型。
func Key(name string, qtype uint16) string {
	return dnsmsg.CanonicalName(name) + "#" + itoa(uint(qtype))
}

// Len 返回当前未过期条目数（会顺带惰性清理）。
func (c *Cache) Len() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.now()
	for k, e := range c.items {
		if !now.Before(e.expiresAt) {
			delete(c.items, k)
		}
	}
	return len(c.items)
}

// Get 返回未过期条目（副本），未命中或已过期返回 nil。
// 命中记录的 TTL 字段会被改写为“剩余 TTL”。
func (c *Cache) Get(key string) *Entry {
	c.mu.RLock()
	e, ok := c.items[key]
	c.mu.RUnlock()
	if !ok {
		return nil
	}

	remaining := e.expiresAt.Sub(c.now())
	if remaining <= 0 {
		c.mu.Lock()
		if cur, ok := c.items[key]; ok && !c.now().Before(cur.expiresAt) {
			delete(c.items, key)
		}
		c.mu.Unlock()
		return nil
	}

	out := *e
	out.Answers = make([]dnsmsg.Record, len(e.Answers))
	copy(out.Answers, e.Answers)
	remainTTL := uint32(remaining / time.Second)
	if remainTTL == 0 {
		remainTTL = 1 // 尚未到过期时刻，至少报告 1 秒
	}
	for i := range out.Answers {
		out.Answers[i].TTL = remainTTL
	}
	out.TTL = remainTTL
	return &out
}

// Set 写入条目。ttlSeconds 为 0 时不缓存（调用方不应为 0，双保险）。
func (c *Cache) Set(key string, e *Entry, ttlSeconds uint32) {
	if ttlSeconds == 0 {
		return
	}
	now := c.now()
	stored := *e
	stored.storedAt = now
	stored.expiresAt = now.Add(time.Duration(ttlSeconds) * time.Second)
	c.mu.Lock()
	c.items[key] = &stored
	c.mu.Unlock()
}

// Delete 清除一个键（测试/管理接口用）。
func (c *Cache) Delete(key string) {
	c.mu.Lock()
	delete(c.items, key)
	c.mu.Unlock()
}

// Flush 清空全部缓存。
func (c *Cache) Flush() {
	c.mu.Lock()
	c.items = make(map[string]*Entry)
	c.mu.Unlock()
}

func itoa(v uint) string {
	if v == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for v > 0 {
		i--
		b[i] = byte('0' + v%10)
		v /= 10
	}
	return string(b[i:])
}
