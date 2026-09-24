package attest

import (
	"sync"
	"time"
)

// ReplayCache 记录已接受的 attestationId，用于拒绝重放。
// 条目在对应证明超出最大年龄后过期清除。
type ReplayCache struct {
	mu   sync.Mutex
	seen map[string]time.Time // id -> 过期时刻
}

func NewReplayCache() *ReplayCache {
	return &ReplayCache{seen: make(map[string]time.Time)}
}

// Add 尝试登记 id；若 id 已存在且未过期则返回 false（重放）。
func (c *ReplayCache) Add(id string, expiry time.Time, now time.Time) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	// 惰性清理过期条目。
	for k, exp := range c.seen {
		if now.After(exp) {
			delete(c.seen, k)
		}
	}
	if _, exists := c.seen[id]; exists {
		return false
	}
	c.seen[id] = expiry
	return true
}
