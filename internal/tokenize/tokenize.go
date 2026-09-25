// Package tokenize turns a raw log line into a sequence of typed slots.
//
// Variables are recognized at this stage:
//   - UUIDs become Slot{Kind: Var, VarKind: UUID}
//   - Numbers (decimal, hex, signed, decimal point, exponent) become Slot{Kind: Var, VarKind: Num}
//   - Quoted strings become Slot{Kind: Var, VarKind: Str}
//
// Everything else is a literal. Alphabetic words are *always* literals, which is
// what keeps different error messages ("Connection refused" vs "Connection timed out")
// from collapsing into the same template. Words that merely contain digits (e.g.
// "8080ms") stay literals here; the cluster layer may later promote that one slot to a
// generic variable once it has seen enough distinct values.
package tokenize

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
)

// MaxTokens bounds the work and memory spent on a single (possibly pathological) line.
// Lines longer than this are tokenized up to the limit and flagged as truncated.
const MaxTokens = 2048

// Kind of a token position: a literal word/punctuation, or a variable.
type Kind int

const (
	Word  Kind = iota // an alphabetic/alphanumeric literal token
	Punct             // a run of non-space, non-word characters
	Var               // a variable slot (see VarKind)
)

func (k Kind) String() string {
	switch k {
	case Word:
		return "word"
	case Punct:
		return "punct"
	case Var:
		return "var"
	default:
		return fmt.Sprintf("kind(%d)", int(k))
	}
}

// VarKind is the concrete type of a Var slot.
type VarKind int

const (
	Num VarKind = iota
	UUID
	Str
	Any // generic variable created by template evolution
)

func (v VarKind) String() string {
	switch v {
	case Num:
		return "num"
	case UUID:
		return "uuid"
	case Str:
		return "str"
	case Any:
		return "any"
	default:
		return fmt.Sprintf("var(%d)", int(v))
	}
}

// Marker renders the way a slot appears in a template string.
func (v VarKind) Marker() string {
	switch v {
	case Num:
		return "<NUM>"
	case UUID:
		return "<UUID>"
	case Str:
		return "<STR>"
	case Any:
		return "<*>"
	default:
		return "<*>"
	}
}

// Slot is one position in a tokenized line or a cluster template.
//
// For Word/Punct positions, Literal holds the exact text. For Var positions,
// Literal is empty and Vk says which variable type the position accepts.
// Sep records whether the original line had whitespace before this token; it
// drives faithful template rendering only and is ignored by matching.
type Slot struct {
	Kind    Kind    `json:"kind"`
	Literal string  `json:"literal,omitempty"`
	Vk      VarKind `json:"vk,omitempty"`
	Sep     bool    `json:"sep,omitempty"`
}

// Result is the output of Line: the slots plus whether the line exceeded MaxTokens.
type Result struct {
	Slots     []Slot
	Truncated bool
}

var (
	// A canonical 8-4-4-4-12 hex UUID. Two shapes are handled separately because
	// Go's RE2 lacks look-around: a brace/urn-bracketed UUID (whose closing '}'
	// must be consumed) and a bare UUID (whose trailing word boundary keeps the
	// first hex group from matching a longer hex string).
	uuidBracedRe = regexp.MustCompile(`(?i)^(?:urn:uuid:\{|\{)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}`)
	uuidBareRe   = regexp.MustCompile(`(?i)^(?:urn:uuid:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b`)
	hexRe        = regexp.MustCompile(`^0[xX][0-9a-fA-F]+(?:[A-Za-z]{1,4})?`)
	// A number with an optional unit suffix (ms, MB, %, KB, s, ...). The suffix
	// is swallowed into the variable slot so "8080ms" and "9090ms" occupy one
	// <NUM> position rather than splitting into <NUM> + word. A trailing
	// letter-only suffix must be short to avoid eating whole words.
	//
	// The *Sign* variants (leading +/-) are used only at a token boundary
	// (start of line or after whitespace). Otherwise the minus in a date like
	// "2026-09-24" would be swallowed as the sign of "-09" and the separator
	// would disappear from the template.
	floatRe     = regexp.MustCompile(`^[0-9]+\.[0-9]+(?:[eE][+-]?[0-9]+)?(?:[A-Za-z%°]{1,4})?`)
	floatSignRe = regexp.MustCompile(`^[+-][0-9]+\.[0-9]+(?:[eE][+-]?[0-9]+)?(?:[A-Za-z%°]{1,4})?`)
	intRe       = regexp.MustCompile(`^[0-9]+(?:[eE][+-]?[0-9]+)?(?:[A-Za-z%°]{1,4})?`)
	intSignRe   = regexp.MustCompile(`^[+-][0-9]+(?:[eE][+-]?[0-9]+)?(?:[A-Za-z%°]{1,4})?`)
	wordRe      = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*`)
	// NOTE: the anchor wraps the whole alternation. Writing it as
	// `^"..."|^'...'` makes the second alternative effectively unanchored in
	// RE2, turning a single quote lookup into an O(n) scan of the whole line.
	quoteRe = regexp.MustCompile(`^(?:"[^"]*"|'[^']*')`)
	punctRe = regexp.MustCompile(`^[^\sA-Za-z0-9_]+`)
	digitRe = regexp.MustCompile(`[0-9]`)
)

// Line tokenizes one log line.
func Line(line string) Result {
	var slots []Slot
	i := 0
	truncated := false
	n := len(line)
	sep := false // whitespace before the next token
	for i < n {
		if line[i] == ' ' || line[i] == '\t' || line[i] == '\r' || line[i] == '\n' {
			sep = true
			i++
			continue
		}
		if len(slots) >= MaxTokens {
			truncated = true
			break
		}
		rest := line[i:]
		var s Slot
		var consumed int

		// 1. UUID, bracketed first so the closing '}' is consumed.
		if loc := uuidBracedRe.FindString(rest); loc != "" {
			s, consumed = Slot{Kind: Var, Vk: UUID}, len(loc)
		} else if loc := uuidBareRe.FindString(rest); loc != "" {
			s, consumed = Slot{Kind: Var, Vk: UUID}, len(loc)
		} else if loc := quoteRe.FindString(rest); loc != "" { // 2. quoted string
			s, consumed = Slot{Kind: Var, Vk: Str}, len(loc)
		} else if loc := hexRe.FindString(rest); loc != "" { // 3. hex number
			s, consumed = Slot{Kind: Var, Vk: Num}, len(loc)
		} else if loc := matchNumber(rest, sep || len(slots) == 0); loc != "" { // 4-5. number
			s, consumed = Slot{Kind: Var, Vk: Num}, len(loc)
		} else if loc := wordRe.FindString(rest); loc != "" { // 6. word
			s, consumed = Slot{Kind: Word, Literal: loc}, len(loc)
		} else if loc := punctRe.FindString(rest); loc != "" { // 7. punctuation
			s, consumed = Slot{Kind: Punct, Literal: loc}, len(loc)
		} else {
			s, consumed = Slot{Kind: Punct, Literal: string(line[i])}, 1
		}
		s.Sep = sep
		slots = append(slots, s)
		sep = false
		i += consumed
	}
	return Result{Slots: slots, Truncated: truncated}
}

// matchNumber recognizes a numeric token at the current position. At a token
// boundary (start of line or following whitespace) a leading sign is allowed;
// elsewhere only unsigned numbers match so separators such as the '-' in a
// date stay punctuation.
func matchNumber(rest string, atBoundary bool) string {
	if atBoundary {
		if loc := floatSignRe.FindString(rest); loc != "" {
			return loc
		}
		if loc := intSignRe.FindString(rest); loc != "" {
			return loc
		}
	}
	if loc := floatRe.FindString(rest); loc != "" {
		return loc
	}
	return intRe.FindString(rest)
}

// Promotable reports whether a literal word slot may be turned into a generic
// variable after repeated distinct observations. Only value-like words qualify:
// they must contain a digit ("8080ms", "v2", "user123") and be short enough that
// they plausibly represent a parameter rather than a sentence fragment. Pure
// alphabetic keywords ("refused", "timeout") never qualify.
func Promotable(s Slot) bool {
	if s.Kind != Word {
		return false
	}
	if !digitRe.MatchString(s.Literal) {
		return false
	}
	return len(s.Literal) <= 32
}

// Render joins template slots back into a human-readable template string.
// Punctuation is glued to its neighbors; words and variable markers are
// space-separated.
// Render joins template slots back into a template string, reproducing the
// original spacing: a slot whose Sep flag is set gets a leading space, a
// punctuation slot attached directly to its neighbor ("/api", "200,") does not.
func Render(slots []Slot) string {
	var b strings.Builder
	for _, s := range slots {
		text := ""
		switch s.Kind {
		case Word:
			text = s.Literal
		case Punct:
			text = s.Literal
		case Var:
			text = s.Vk.Marker()
		}
		if s.Sep {
			b.WriteByte(' ')
		}
		b.WriteString(text)
	}
	return b.String()
}

// JSON helpers (used by the persistence layer through encoding/json).

func (k Kind) MarshalJSON() ([]byte, error) { return json.Marshal(k.String()) }

func (v VarKind) MarshalJSON() ([]byte, error) { return json.Marshal(v.String()) }

func (k *Kind) UnmarshalJSON(data []byte) error {
	var s string
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	switch s {
	case "word":
		*k = Word
	case "punct":
		*k = Punct
	case "var":
		*k = Var
	default:
		return fmt.Errorf("unknown slot kind %q", s)
	}
	return nil
}

func (v *VarKind) UnmarshalJSON(data []byte) error {
	var s string
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	switch s {
	case "num":
		*v = Num
	case "uuid":
		*v = UUID
	case "str":
		*v = Str
	case "any":
		*v = Any
	default:
		return fmt.Errorf("unknown var kind %q", s)
	}
	return nil
}
