package cluster

import (
	"fmt"
	"strings"
)

// Wildcard is the generalized token used when a position has seen more than
// one variable type, or a literal where a variable was expected.
const Wildcard = "*"

// VersionRecord describes one template update. Version 1 is the creation
// snapshot; every later record is a generalization step.
type VersionRecord struct {
	Version int    `json:"version"`
	Seq     int64  `json:"seq"`
	Pattern string `json:"pattern"`
	Reason  string `json:"reason"`
}

// Template is a cluster of log lines sharing a token pattern.
type Template struct {
	ID         int64           `json:"id"`
	Tokens     []string        `json:"tokens"`
	Pattern    string          `json:"pattern"`
	Count      int64           `json:"count"`
	Version    int             `json:"version"`
	CreatedSeq int64           `json:"created_seq"`
	LastSeq    int64           `json:"last_seq"`
	History    []VersionRecord `json:"history"`
}

func newTemplate(id, seq int64, tokens []string) *Template {
	t := &Template{
		ID:         id,
		Tokens:     append([]string(nil), tokens...),
		Count:      0,
		Version:    1,
		CreatedSeq: seq,
		LastSeq:    seq,
	}
	t.Pattern = strings.Join(t.Tokens, " ")
	t.History = []VersionRecord{{Version: 1, Seq: seq, Pattern: t.Pattern, Reason: "created"}}
	return t
}

// matchLog checks whether masked log tokens fit this template. On success it
// returns the (possibly generalized) token list and whether it changed.
//
// Merge rules, per position:
//   - equal tokens (including identical variables) always match;
//   - an existing wildcard matches anything;
//   - two different variable types generalize to a wildcard;
//   - a literal in the template where the log has a variable generalizes to
//     a wildcard;
//   - two DIFFERENT literals never match: keyword differences (e.g.
//     "read" vs "write", "timeout" vs "refused") always stay in separate
//     templates, which is the guard against over-generalization.
//
// A generalization that would push the wildcard ratio above maxWildRatio is
// rejected, so the candidate keeps its own template instead.
func (t *Template) matchLog(log []string, maxWildRatio float64) (bool, []string, bool) {
	if len(t.Tokens) != len(log) {
		return false, nil, false
	}
	out := make([]string, len(t.Tokens))
	changed := false
	for i := range t.Tokens {
		tok, lt := t.Tokens[i], log[i]
		switch {
		case tok == lt || tok == Wildcard:
			out[i] = tok
		case IsVar(tok) && IsVar(lt):
			out[i] = Wildcard
			changed = true
		case IsVar(lt): // literal in template, variable in log
			out[i] = Wildcard
			changed = true
		default:
			return false, nil, false
		}
	}
	if changed && wildcards(out) > maxWildcards(len(out), maxWildRatio) {
		return false, nil, false
	}
	return true, out, changed
}

// applyUpdate installs generalized tokens and bumps the version.
func (t *Template) applyUpdate(tokens []string, seq int64) {
	t.Tokens = tokens
	t.Pattern = strings.Join(tokens, " ")
	t.Version++
	t.History = append(t.History, VersionRecord{
		Version: t.Version,
		Seq:     seq,
		Pattern: t.Pattern,
		Reason:  "generalized",
	})
}

func wildcards(tokens []string) int {
	n := 0
	for _, t := range tokens {
		if t == Wildcard {
			n++
		}
	}
	return n
}

// maxWildcards caps how many positions of a length-n template may become
// wildcards. Position 0 is the bucket key and never wildcards in practice,
// but the ratio alone is the guard.
func maxWildcards(n int, ratio float64) int {
	if ratio <= 0 {
		ratio = DefaultMaxWildRatio
	}
	m := int(float64(n) * ratio)
	if m < 1 {
		m = 1
	}
	return m
}

func (t *Template) String() string {
	return fmt.Sprintf("#%d v%d x%d %s", t.ID, t.Version, t.Count, t.Pattern)
}
