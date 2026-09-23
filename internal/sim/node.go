package sim

import (
	"sort"

	"chsim/internal/ring"
)

// Node is an in-process simulation of one storage node. Its store is a plain
// map; all access happens from the single event-loop goroutine, so no locks
// are needed.
type Node struct {
	id    string
	store map[string]Record
	alive bool
}

func newNode(id string) *Node {
	return &Node{id: id, store: make(map[string]Record), alive: true}
}

// putIfNewer applies a versioned write: the highest version seen wins, and
// equal versions are byte-identical idempotent retries.
func (n *Node) putIfNewer(key string, rec Record) bool {
	if !n.alive {
		return false
	}
	cur, ok := n.store[key]
	if !ok || rec.Version > cur.Version {
		n.store[key] = rec
		return true
	}
	return false
}

// scanRange returns one deterministic page of the node's keys whose hash lies
// in (start, end]. Keys are sorted so pagination and re-runs are stable.
func (n *Node) scanRange(t ring.Task, cursor, limit int) (page []kv, nextCursor int, done bool) {
	var keys []string
	for k := range n.store {
		if t.Contains(ring.HashKey(k)) {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	if cursor > len(keys) {
		cursor = len(keys)
	}
	end := cursor + limit
	if end >= len(keys) {
		end = len(keys)
		done = true
	}
	for _, k := range keys[cursor:end] {
		rec := n.store[k]
		page = append(page, kv{Key: k, Rec: rec})
	}
	return page, end, done
}
