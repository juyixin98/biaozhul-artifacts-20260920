package server

import (
	"sync"

	"github.com/example/merklekv/internal/merkle"
)

// treeCache memoizes the built Merkle tree for each retained snapshot. It is
// a tiny LRU keyed by snapshot id: snapshots are immutable, so the cached
// tree is valid for the snapshot's whole lifetime. When the store evicts a
// snapshot the entry becomes stale garbage and is pushed out by capacity.
type treeCache struct {
	mu    sync.Mutex
	cap   int
	ids   []string // MRU first
	trees map[string]*merkle.Tree
}

func newTreeCache(capacity int) *treeCache {
	return &treeCache{cap: capacity, trees: make(map[string]*merkle.Tree)}
}

func (c *treeCache) get(id string, build func() *merkle.Tree) *merkle.Tree {
	c.mu.Lock()
	defer c.mu.Unlock()
	if t, ok := c.trees[id]; ok {
		c.touchLocked(id)
		return t
	}
	t := build()
	c.ids = append([]string{id}, c.ids...)
	c.trees[id] = t
	for len(c.ids) > c.cap {
		victim := c.ids[len(c.ids)-1]
		c.ids = c.ids[:len(c.ids)-1]
		delete(c.trees, victim)
	}
	return t
}

func (c *treeCache) evict(id string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.trees, id)
	for i, x := range c.ids {
		if x == id {
			c.ids = append(c.ids[:i], c.ids[i+1:]...)
			break
		}
	}
}

func (c *treeCache) evictAll() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.ids = nil
	c.trees = make(map[string]*merkle.Tree)
}

func (c *treeCache) touchLocked(id string) {
	for i, x := range c.ids {
		if x == id {
			copy(c.ids[1:i+1], c.ids[0:i])
			c.ids[0] = id
			return
		}
	}
}
