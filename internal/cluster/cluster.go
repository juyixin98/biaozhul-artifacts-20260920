package cluster

import (
	"errors"
	"sort"
	"strings"
)

// Default bounds. All are configurable via Config.
const (
	DefaultMaxTemplates = 10000
	DefaultMaxLineBytes = 64 * 1024
	DefaultMaxTokens    = 256
	DefaultMaxWildRatio = 0.4
)

// ErrEmptyLine is returned for whitespace-only input.
var ErrEmptyLine = errors.New("empty log line")

// Config tunes a Clusterer. Zero fields fall back to the Default* constants.
type Config struct {
	MaxTemplates int     // hard capacity; least-recently-seen template is evicted
	MaxLineBytes int     // longer lines are truncated and marked <TRUNC>
	MaxTokens    int     // longer token lists are truncated and marked <TRUNC>
	MaxWildRatio float64 // max fraction of wildcard tokens per template
}

func (c Config) withDefaults() Config {
	if c.MaxTemplates <= 0 {
		c.MaxTemplates = DefaultMaxTemplates
	}
	if c.MaxLineBytes <= 0 {
		c.MaxLineBytes = DefaultMaxLineBytes
	}
	if c.MaxTokens <= 0 {
		c.MaxTokens = DefaultMaxTokens
	}
	if c.MaxWildRatio <= 0 {
		c.MaxWildRatio = DefaultMaxWildRatio
	}
	return c
}

// Assignment is the result of ingesting one line.
type Assignment struct {
	TemplateID int64  `json:"template_id"`
	Pattern    string `json:"pattern"`
	Version    int    `json:"version"`
	Created    bool   `json:"created"` // a new template was created for this line
}

// Eviction records a capacity-driven removal.
type Eviction struct {
	TemplateID int64  `json:"template_id"`
	Pattern    string `json:"pattern"`
	Seq        int64  `json:"seq"`
}

// bucketKey groups candidate templates: same token count, same first token.
type bucketKey struct {
	length int
	first  string
}

// Clusterer is a bounded online log-template clusterer. It is not safe for
// concurrent use; the server layer serializes access.
type Clusterer struct {
	cfg       Config
	buckets   map[bucketKey][]*Template
	byID      map[int64]*Template
	seq       int64 // monotonic ingest sequence, also the LRU clock
	nextID    int64
	evictions []Eviction
}

func New(cfg Config) *Clusterer {
	return &Clusterer{
		cfg:     cfg.withDefaults(),
		buckets: make(map[bucketKey][]*Template),
		byID:    make(map[int64]*Template),
		nextID:  1,
	}
}

// Config returns the effective configuration (defaults applied).
func (c *Clusterer) Config() Config { return c.cfg }

// Ingest tokenizes one raw line and assigns it to a template, creating or
// generalizing templates as needed.
func (c *Clusterer) Ingest(line string) (Assignment, error) {
	tokens := Tokenize(line, c.cfg.MaxLineBytes, c.cfg.MaxTokens)
	if len(tokens) == 0 {
		return Assignment{}, ErrEmptyLine
	}
	c.seq++
	key := bucketKey{length: len(tokens), first: tokens[0]}

	var best *Template
	var bestTokens []string
	bestChanged := false
	for _, t := range c.buckets[key] {
		ok, out, changed := t.matchLog(tokens, c.cfg.MaxWildRatio)
		if !ok {
			continue
		}
		// Deterministic preference: exact reuse beats generalization; ties go
		// to the lowest template ID.
		if best == nil || (!changed && bestChanged) {
			best, bestTokens, bestChanged = t, out, changed
			if !changed {
				break
			}
		}
	}

	if best != nil {
		if bestChanged {
			best.applyUpdate(bestTokens, c.seq)
		}
		best.Count++
		best.LastSeq = c.seq
		return Assignment{TemplateID: best.ID, Pattern: best.Pattern, Version: best.Version}, nil
	}

	t := newTemplate(c.nextID, c.seq, tokens)
	c.nextID++
	c.buckets[key] = append(c.buckets[key], t)
	c.byID[t.ID] = t
	t.Count = 1

	if len(c.byID) > c.cfg.MaxTemplates {
		c.evictLRU()
	}
	return Assignment{TemplateID: t.ID, Pattern: t.Pattern, Version: t.Version, Created: true}, nil
}

// evictLRU removes the template with the smallest LastSeq; ties break to the
// lowest ID, keeping eviction fully deterministic.
func (c *Clusterer) evictLRU() {
	var victim *Template
	for _, t := range c.byID {
		if victim == nil || t.LastSeq < victim.LastSeq ||
			(t.LastSeq == victim.LastSeq && t.ID < victim.ID) {
			victim = t
		}
	}
	if victim == nil {
		return
	}
	key := bucketKey{length: len(victim.Tokens), first: victim.Tokens[0]}
	list := c.buckets[key]
	for i, t := range list {
		if t.ID == victim.ID {
			c.buckets[key] = append(list[:i:i], list[i+1:]...)
			break
		}
	}
	if len(c.buckets[key]) == 0 {
		delete(c.buckets, key)
	}
	delete(c.byID, victim.ID)
	c.evictions = append(c.evictions, Eviction{TemplateID: victim.ID, Pattern: victim.Pattern, Seq: c.seq})
}

// Templates returns all live templates sorted by ID.
func (c *Clusterer) Templates() []*Template {
	out := make([]*Template, 0, len(c.byID))
	for _, t := range c.byID {
		out = append(out, t)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// Get returns one template by ID.
func (c *Clusterer) Get(id int64) (*Template, bool) {
	t, ok := c.byID[id]
	return t, ok
}

// Evictions returns the eviction log in order.
func (c *Clusterer) Evictions() []Eviction {
	out := make([]Eviction, len(c.evictions))
	copy(out, c.evictions)
	return out
}

// Stats summarizes the clusterer state.
type Stats struct {
	Templates int   `json:"templates"`
	Evictions int   `json:"evictions"`
	Ingested  int64 `json:"ingested"`
	NextID    int64 `json:"next_id"`
	Capacity  int   `json:"capacity"`
}

func (c *Clusterer) Stats() Stats {
	return Stats{
		Templates: len(c.byID),
		Evictions: len(c.evictions),
		Ingested:  c.seq,
		NextID:    c.nextID,
		Capacity:  c.cfg.MaxTemplates,
	}
}

// Join is a helper for tests and eval output.
func Join(tokens []string) string { return strings.Join(tokens, " ") }
