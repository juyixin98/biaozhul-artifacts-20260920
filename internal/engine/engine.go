// Package engine ties tokenization, template clusters, bounded retention and
// the event store together. Ingest is online and deterministic: the same input
// stream always produces the same cluster IDs, templates and versions.
package engine

import (
	"errors"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"logcluster/internal/cluster"
	"logcluster/internal/ring"
	"logcluster/internal/tokenize"
)

// Clock allows tests to pin time; production uses WallClock.
type Clock interface {
	Now() time.Time
}

// WallClock is the real system clock.
type WallClock struct{}

func (WallClock) Now() time.Time { return time.Now().UTC() }

// Config bounds every unbounded input: number of templates, stored events,
// line length and remembered evictions.
type Config struct {
	MaxClusters   int
	RingCapacity  int
	MaxLineBytes  int
	MaxEvictedLog int
}

// DefaultConfig returns the production bounds.
func DefaultConfig() Config {
	return Config{
		MaxClusters:   500,
		RingCapacity:  5000,
		MaxLineBytes:  16 * 1024,
		MaxEvictedLog: 100,
	}
}

// EvictedEntry records a template retired under capacity pressure.
type EvictedEntry struct {
	ClusterID int       `json:"cluster_id"`
	Template  string    `json:"template"`
	Count     int64     `json:"count"`
	EvictedAt time.Time `json:"evicted_at"`
}

// TemplateInfo is the API-facing view of a cluster.
type TemplateInfo struct {
	ID        int               `json:"id"`
	Template  string            `json:"template"`
	Version   int               `json:"version"`
	Count     int64             `json:"count"`
	FirstSeen time.Time         `json:"first_seen"`
	LastSeen  time.Time         `json:"last_seen"`
	Samples   []string          `json:"samples"`
	Versions  []cluster.Version `json:"versions"`
}

// Stats are the engine counters used by /v1/metrics.
type Stats struct {
	TotalLines      int64     `json:"total_lines"`
	TruncatedLines  int64     `json:"truncated_lines"`
	ActiveClusters  int       `json:"active_clusters"`
	ClusterCapacity int       `json:"cluster_capacity"`
	EvictedClusters int64     `json:"evicted_clusters"`
	StoredEvents    int       `json:"stored_events"`
	EventCapacity   int       `json:"event_capacity"`
	LastSeq         int64     `json:"last_seq"`
	LastSeen        time.Time `json:"last_seen"`
}

// Engine is the thread-safe online clustering engine.
type Engine struct {
	mu sync.Mutex

	cfg        Config
	clock      Clock
	clusters   map[int]*cluster.Cluster
	events     *ring.Ring
	nextID     int
	totalLines int64
	truncated  int64
	evictedN   int64
	evicted    []EvictedEntry
	lastSeen   time.Time
}

// New builds an engine. A nil clock uses the wall clock.
func New(cfg Config, c Clock) *Engine {
	if c == nil {
		c = WallClock{}
	}
	return &Engine{
		cfg:      cfg,
		clock:    c,
		clusters: map[int]*cluster.Cluster{},
		events:   ring.New(cfg.RingCapacity),
		nextID:   1,
	}
}

// Ingest tokenizes one line, assigns it to a template cluster (creating or
// evolving one as needed) and records it in the event store. The returned
// event carries the cluster assignment and template version.
func (e *Engine) Ingest(raw string) (ring.Event, error) {
	line := strings.TrimSpace(raw)
	if line == "" {
		return ring.Event{}, errors.New("empty log line")
	}
	byteClipped := false
	if e.cfg.MaxLineBytes > 0 && len(line) > e.cfg.MaxLineBytes {
		line = clipBytes(line, e.cfg.MaxLineBytes)
		byteClipped = true
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	now := e.clock.Now()
	e.lastSeen = now
	e.totalLines++

	res := tokenize.Line(line)
	if res.Truncated || byteClipped {
		e.truncated++
	}

	c, nearPos := e.assign(res.Slots, now)
	c.Absorb(line, res.Slots, now, nearPos)

	ev := e.events.Add(ring.Event{
		Time:      now,
		Line:      line,
		ClusterID: c.ID,
		Template:  c.Template(),
		Version:   c.Version,
	})
	return ev, nil
}

// creates a new one (evicting an LRU template at capacity). A nearPos >= 0
// return value asks the caller to evolve that slot to <*> during Absorb.
func (e *Engine) assign(toks []tokenize.Slot, now time.Time) (*cluster.Cluster, int) {
	ids := sortedKeys(e.clusters)

	var exact *cluster.Cluster
	var near *cluster.Cluster
	nearPos := -1
	for _, id := range ids {
		c := e.clusters[id]
		mm, sameLength := c.MatchMismatches(toks)
		if !sameLength {
			continue // different length
		}
		if len(mm) == 0 {
			exact = c
			break
		}
		if near == nil && len(mm) == 1 && c.PromotableAt(mm[0], toks) {
			near = c
			nearPos = mm[0]
		}
	}

	if exact != nil {
		return exact, -1
	}
	if near != nil {
		return near, nearPos
	}

	if len(e.clusters) >= e.cfg.MaxClusters {
		e.evictLRU(now)
	}
	c := cluster.New(e.nextID, toks, now)
	e.nextID++
	e.clusters[c.ID] = c
	return c, -1
}

// evictLRU retires the least recently used cluster; ties break on lower ID.
func (e *Engine) evictLRU(now time.Time) {
	victimID := -1
	var victimLast time.Time
	for id, c := range e.clusters {
		if victimID == -1 || c.LastSeen.Before(victimLast) ||
			(c.LastSeen.Equal(victimLast) && id < victimID) {
			victimID = id
			victimLast = c.LastSeen
		}
	}
	c := e.clusters[victimID]
	delete(e.clusters, victimID)
	e.evictedN++
	e.evicted = append(e.evicted, EvictedEntry{
		ClusterID: c.ID,
		Template:  c.Template(),
		Count:     c.Count,
		EvictedAt: now,
	})
	if e.cfg.MaxEvictedLog >= 0 && len(e.evicted) > e.cfg.MaxEvictedLog {
		e.evicted = e.evicted[len(e.evicted)-e.cfg.MaxEvictedLog:]
	}
}

// Templates returns all active template clusters ordered by ID.
func (e *Engine) Templates() []TemplateInfo {
	e.mu.Lock()
	defer e.mu.Unlock()
	ids := sortedKeys(e.clusters)
	out := make([]TemplateInfo, 0, len(ids))
	for _, id := range ids {
		c := e.clusters[id]
		out = append(out, TemplateInfo{
			ID:        c.ID,
			Template:  c.Template(),
			Version:   c.Version,
			Count:     c.Count,
			FirstSeen: c.FirstSeen,
			LastSeen:  c.LastSeen,
			Samples:   append([]string(nil), c.Samples...),
			Versions:  append([]cluster.Version(nil), c.Versions...),
		})
	}
	return out
}

// Cluster returns one template cluster and whether it exists.
func (e *Engine) Cluster(id int) (TemplateInfo, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	c, ok := e.clusters[id]
	if !ok {
		return TemplateInfo{}, false
	}
	return TemplateInfo{
		ID:        c.ID,
		Template:  c.Template(),
		Version:   c.Version,
		Count:     c.Count,
		FirstSeen: c.FirstSeen,
		LastSeen:  c.LastSeen,
		Samples:   append([]string(nil), c.Samples...),
		Versions:  append([]cluster.Version(nil), c.Versions...),
	}, true
}

// Query proxies to the event store.
func (e *Engine) Query(clusterID int, hasCluster bool, substring string, limit int) []ring.Event {
	return e.events.Query(clusterID, hasCluster, substring, limit)
}

// Evicted returns the bounded list of retired templates.
func (e *Engine) Evicted() []EvictedEntry {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]EvictedEntry(nil), e.evicted...)
}

// Stats returns current counters.
func (e *Engine) Stats() Stats {
	e.mu.Lock()
	defer e.mu.Unlock()
	stored, capEv, lastSeq := e.events.Stats()
	return Stats{
		TotalLines:      e.totalLines,
		TruncatedLines:  e.truncated,
		ActiveClusters:  len(e.clusters),
		ClusterCapacity: e.cfg.MaxClusters,
		EvictedClusters: e.evictedN,
		StoredEvents:    stored,
		EventCapacity:   capEv,
		LastSeq:         lastSeq,
		LastSeen:        e.lastSeen,
	}
}

func sortedKeys(m map[int]*cluster.Cluster) []int {
	ids := make([]int, 0, len(m))
	for id := range m {
		ids = append(ids, id)
	}
	sort.Ints(ids)
	return ids
}

// clipBytes cuts s to at most n bytes without splitting a UTF-8 rune.
func clipBytes(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && !utf8.RuneStart(s[n]) {
		n--
	}
	return s[:n]
}
