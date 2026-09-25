// Package cluster holds a single template cluster and the matching rules that
// decide whether a tokenized log line belongs to it.
//
// Matching policy (the core "avoid over-generalization" rule):
//   - template and line must have the same number of slots;
//   - a literal slot accepts only an identical literal token;
//   - a typed variable slot (<NUM>/<UUID>/<STR>) accepts only that token type;
//   - the generic <*> slot accepts any single token.
//
// Type mismatches never silently merge: a UUID at a <NUM> position is a miss,
// and a literal word ("refused" vs "timeout") is always a miss.
package cluster

import (
	"fmt"
	"time"

	"logcluster/internal/tokenize"
)

// MaxSamples is the bounded number of raw example lines kept per cluster.
const MaxSamples = 3

// maxDistinct bounds memory used to track distinct values at a literal position.
// Promotion triggers at PromotionDistinct (2), so beyond this cap the position
// is simply considered saturated.
const maxDistinct = 32

// PromotionDistinct is the number of different value-like words a literal slot
// must see before it evolves into a generic <*> variable. 1 never promotes
// (that would collapse a typo/rare event into an existing template); 2 gives
// "see the same shape twice with different values" evidence.
const PromotionDistinct = 2

// Version records one revision of a cluster's template.
type Version struct {
	Number    int       `json:"number"`
	Template  string    `json:"template"`
	CreatedAt time.Time `json:"created_at"`
	Reason    string    `json:"reason,omitempty"`
}

// Cluster is one bounded template group.
type Cluster struct {
	ID        int             `json:"id"`
	Slots     []tokenize.Slot `json:"slots"`
	Version   int             `json:"version"`
	Versions  []Version       `json:"versions"`
	Count     int64           `json:"count"`
	FirstSeen time.Time       `json:"first_seen"`
	LastSeen  time.Time       `json:"last_seen"`
	Samples   []string        `json:"samples"`

	// Distinct values seen at each still-literal, promotable position.
	// Indexed by slot position; nil entries mean the position does not track values.
	distinct []map[string]struct{}
}

// New creates a cluster with version 1 of its template.
func New(id int, slots []tokenize.Slot, now time.Time) *Cluster {
	// Copy slots so later caller mutations cannot corrupt the cluster.
	cp := make([]tokenize.Slot, len(slots))
	copy(cp, slots)
	c := &Cluster{
		ID:        id,
		Slots:     cp,
		Version:   1,
		Count:     0,
		FirstSeen: now,
		LastSeen:  now,
	}
	c.Versions = []Version{{
		Number:    1,
		Template:  c.Template(),
		CreatedAt: now,
		Reason:    "created",
	}}
	c.initDistinct()
	return c
}

// Template renders the current slot sequence as a template string.
func (c *Cluster) Template() string { return tokenize.Render(c.Slots) }

func (c *Cluster) initDistinct() {
	c.distinct = make([]map[string]struct{}, len(c.Slots))
	for i, s := range c.Slots {
		if tokenize.Promotable(s) {
			c.distinct[i] = map[string]struct{}{}
		}
	}
}

// slotAccept reports whether template slot a accepts incoming token b.
func slotAccept(a, b tokenize.Slot) bool {
	switch a.Kind {
	case tokenize.Var:
		switch a.Vk {
		case tokenize.Any:
			return true // <*> accepts any single token
		case tokenize.Num:
			return b.Kind == tokenize.Var && b.Vk == tokenize.Num
		case tokenize.UUID:
			return b.Kind == tokenize.Var && b.Vk == tokenize.UUID
		case tokenize.Str:
			return b.Kind == tokenize.Var && b.Vk == tokenize.Str
		}
		return false
	case tokenize.Word:
		return b.Kind == tokenize.Word && b.Literal == a.Literal
	case tokenize.Punct:
		return b.Kind == tokenize.Punct && b.Literal == a.Literal
	}
	return false
}

// MatchMismatches returns the positions at which the incoming slots fail to
// match. The boolean reports whether lengths are equal: a false value means no
// positional comparison was possible; a true value with an empty slice means
// an exact match.
func (c *Cluster) MatchMismatches(toks []tokenize.Slot) ([]int, bool) {
	if len(toks) != len(c.Slots) {
		return nil, false
	}
	var mm []int
	for i, a := range c.Slots {
		if !slotAccept(a, toks[i]) {
			mm = append(mm, i)
		}
	}
	return mm, true
}

// Matches is the exact-match predicate.
func (c *Cluster) Matches(toks []tokenize.Slot) bool {
	mm, ok := c.MatchMismatches(toks)
	return ok && len(mm) == 0
}

// PromotableAt reports whether a mismatch at pos could be healed by evolving
// this cluster's template: both sides are value-like literal words.
func (c *Cluster) PromotableAt(pos int, toks []tokenize.Slot) bool {
	if pos < 0 || pos >= len(c.Slots) || pos >= len(toks) {
		return false
	}
	return tokenize.Promotable(c.Slots[pos]) && tokenize.Promotable(toks[pos])
}

// Absorb records a matching line in the cluster. nearPos >= 0 means the line
// matched only after evolving position nearPos into a generic <*> slot; in that
// case a new template version is appended.
func (c *Cluster) Absorb(line string, toks []tokenize.Slot, now time.Time, nearPos int) {
	c.Count++
	c.LastSeen = now
	if c.Count == 1 {
		c.FirstSeen = now
	}
	c.addSample(line)

	if nearPos >= 0 {
		c.promote(nearPos, now)
		return
	}

	// Record distinct values at literal, value-like positions; once a slot has
	// seen PromotionDistinct different values, self-evolve (e.g. the cluster was
	// born from a line whose "8080ms" happened to be literal).
	for i, s := range c.Slots {
		d := c.distinct[i]
		if d == nil || i >= len(toks) {
			continue
		}
		if !tokenize.Promotable(s) {
			c.distinct[i] = nil
			continue
		}
		t := toks[i]
		if t.Kind != tokenize.Word {
			continue
		}
		d[t.Literal] = struct{}{}
		if len(d) >= PromotionDistinct {
			c.promote(i, now)
		}
	}
}

// promote evolves a single literal position into a generic <*> variable,
// keeping its original leading-space flag so the rendered template is
// unchanged apart from the marker, and appends a new template version.
func (c *Cluster) promote(pos int, now time.Time) {
	sep := c.Slots[pos].Sep
	c.Slots[pos] = tokenize.Slot{Kind: tokenize.Var, Vk: tokenize.Any, Sep: sep}
	c.distinct[pos] = nil
	c.Version++
	c.Versions = append(c.Versions, Version{
		Number:    c.Version,
		Template:  c.Template(),
		CreatedAt: now,
		Reason:    fmt.Sprintf("promoted position %d to <*>", pos),
	})
}

func (c *Cluster) addSample(line string) {
	for _, s := range c.Samples {
		if s == line {
			return
		}
	}
	if len(c.Samples) < MaxSamples {
		c.Samples = append(c.Samples, line)
		return
	}
	// Bounded: drop the middle sample and keep the newest, so examples stay
	// cheap and the latest shape is visible.
	copy(c.Samples[1:], c.Samples[2:])
	c.Samples[MaxSamples-1] = line
}

// Rehydrate rebuilds internal state that is not part of the serialized form.
// Distinct-value evidence for promotion is intentionally not persisted:
// after a restart, promotion evidence is rebuilt from new observations.
func (c *Cluster) Rehydrate() {
	c.initDistinct()
}
