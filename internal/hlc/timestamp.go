// Package hlc implements a Hybrid Logical Clock (Kulkarni et al., 2014).
//
// A timestamp is a pair (physical, logical) plus the originating node id:
//   - physical is a physical-clock reading in Unix milliseconds (int64);
//   - logical  is a logical counter used to break ties inside one millisecond
//     and to survive physical clock rollback. It is carried as uint64 on the
//     wire and bounded at runtime by the clock's MaxLogical (default 2^32-1).
//
// Timestamps are totally ordered lexicographically by (physical, logical);
// the node id is metadata and never participates in comparison or merging.
package hlc

import (
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// DefaultMaxLogical is the default logical-counter limit (uint32 max).
const DefaultMaxLogical uint64 = 1<<32 - 1

// WirePrefix prefixes the canonical text form "hlc://<node>/<physical>:<logical>".
const WirePrefix = "hlc://"

var (
	// ErrInvalidTimestamp is returned when a remote timestamp fails
	// validation: a bad node id, a negative physical part, or a malformed
	// wire form.
	ErrInvalidTimestamp = errors.New("hlc: invalid timestamp")
)

// Timestamp is one HLC timestamp. Numeric fields are serialised as exact JSON
// integers; a "wire" string carrying the identical numbers is emitted
// alongside them so IEEE-754 clients (e.g. browser JS) never lose precision.
type Timestamp struct {
	Physical int64  `json:"physical_ms"`
	Logical  uint64 `json:"logical"`
	NodeID   string `json:"node_id"`
}

// Compare orders two timestamps by (physical, logical). It returns -1, 0 or 1.
// The node id is deliberately ignored: it identifies the origin but does not
// affect causal order.
func (t Timestamp) Compare(o Timestamp) int {
	switch {
	case t.Physical < o.Physical:
		return -1
	case t.Physical > o.Physical:
		return 1
	case t.Logical < o.Logical:
		return -1
	case t.Logical > o.Logical:
		return 1
	default:
		return 0
	}
}

// Less reports whether t causally precedes o.
func (t Timestamp) Less(o Timestamp) bool { return t.Compare(o) < 0 }

// Equal reports identical physical and logical parts (node ignored).
func (t Timestamp) Equal(o Timestamp) bool { return t.Compare(o) == 0 }

// Validate checks a timestamp received over the wire.
func (t Timestamp) Validate() error {
	if err := ValidateNodeID(t.NodeID); err != nil {
		return err
	}
	if t.Physical < 0 {
		return fmt.Errorf("%w: physical must be >= 0", ErrInvalidTimestamp)
	}
	return nil
}

// ValidateNodeID enforces non-empty node ids without ':' or whitespace: ':'
// delimits the canonical wire form and whitespace would break log lines.
func ValidateNodeID(id string) error {
	if id == "" {
		return fmt.Errorf("%w: empty node id", ErrInvalidTimestamp)
	}
	if strings.ContainsAny(id, " \t\r\n:") {
		return fmt.Errorf("%w: node id %q contains ':' or whitespace", ErrInvalidTimestamp, id)
	}
	if len(id) > 256 {
		return fmt.Errorf("%w: node id too long", ErrInvalidTimestamp)
	}
	return nil
}

// Wire renders the canonical text form "hlc://node/<physical>:<logical>".
func (t Timestamp) Wire() string {
	return fmt.Sprintf("%s%s/%d:%d", WirePrefix, t.NodeID, t.Physical, t.Logical)
}

// String returns the canonical wire form (an empty node renders as "-").
func (t Timestamp) String() string {
	node := t.NodeID
	if node == "" {
		node = "-"
	}
	return fmt.Sprintf("%s%s/%d:%d", WirePrefix, node, t.Physical, t.Logical)
}

// ParseWire parses "hlc://node/<physical>:<logical>".
func ParseWire(s string) (Timestamp, error) {
	rest, ok := strings.CutPrefix(s, WirePrefix)
	if !ok {
		return Timestamp{}, fmt.Errorf("%w: missing %q prefix", ErrInvalidTimestamp, WirePrefix)
	}
	node, tail, ok := strings.Cut(rest, "/")
	if !ok {
		return Timestamp{}, fmt.Errorf("%w: missing '/'", ErrInvalidTimestamp)
	}
	physS, logS, ok := strings.Cut(tail, ":")
	if !ok {
		return Timestamp{}, fmt.Errorf("%w: missing ':'", ErrInvalidTimestamp)
	}
	phys, err := strconv.ParseInt(physS, 10, 64)
	if err != nil {
		return Timestamp{}, fmt.Errorf("%w: bad physical: %v", ErrInvalidTimestamp, err)
	}
	logical, err := strconv.ParseUint(logS, 10, 64)
	if err != nil {
		return Timestamp{}, fmt.Errorf("%w: bad logical: %v", ErrInvalidTimestamp, err)
	}
	t := Timestamp{Physical: phys, Logical: logical, NodeID: node}
	if err := t.Validate(); err != nil {
		return Timestamp{}, err
	}
	return t, nil
}

// flexInt64 accepts a JSON integer or a quoted decimal string.
type flexInt64 int64

func (f *flexInt64) UnmarshalJSON(b []byte) error {
	if len(b) == 0 || string(b) == "null" {
		return fmt.Errorf("%w: physical_ms is required", ErrInvalidTimestamp)
	}
	if b[0] == '"' {
		var s string
		if err := json.Unmarshal(b, &s); err != nil {
			return err
		}
		v, err := strconv.ParseInt(s, 10, 64)
		if err != nil {
			return fmt.Errorf("%w: bad physical_ms string: %v", ErrInvalidTimestamp, err)
		}
		*f = flexInt64(v)
		return nil
	}
	v, err := strconv.ParseInt(string(b), 10, 64)
	if err != nil {
		return fmt.Errorf("%w: bad physical_ms: %v", ErrInvalidTimestamp, err)
	}
	*f = flexInt64(v)
	return nil
}

// flexUint64 accepts a JSON integer or a quoted decimal string.
type flexUint64 uint64

func (f *flexUint64) UnmarshalJSON(b []byte) error {
	if len(b) == 0 || string(b) == "null" {
		return fmt.Errorf("%w: logical is required", ErrInvalidTimestamp)
	}
	if b[0] == '"' {
		var s string
		if err := json.Unmarshal(b, &s); err != nil {
			return err
		}
		v, err := strconv.ParseUint(s, 10, 64)
		if err != nil {
			return fmt.Errorf("%w: bad logical string: %v", ErrInvalidTimestamp, err)
		}
		*f = flexUint64(v)
		return nil
	}
	v, err := strconv.ParseUint(string(b), 10, 64)
	if err != nil {
		return fmt.Errorf("%w: bad logical: %v", ErrInvalidTimestamp, err)
	}
	*f = flexUint64(v)
	return nil
}

// MarshalJSON emits the numeric fields plus the exact canonical "wire" string.
func (t Timestamp) MarshalJSON() ([]byte, error) {
	type alias Timestamp
	return json.Marshal(struct {
		alias
		Wire string `json:"wire"`
	}{
		alias: alias(t),
		Wire:  t.Wire(),
	})
}

// UnmarshalJSON accepts:
//
//	{"physical_ms":1700,"logical":3,"node_id":"a"}
//	{"physical_ms":"1700","logical":"3","node_id":"a"}  (quoted numbers)
//	"hlc://a/1700:3"                                     (canonical text)
func (t *Timestamp) UnmarshalJSON(b []byte) error {
	trimmed := strings.TrimSpace(string(b))
	if strings.HasPrefix(trimmed, `"`) {
		var s string
		if err := json.Unmarshal(b, &s); err != nil {
			return err
		}
		parsed, err := ParseWire(s)
		if err != nil {
			return err
		}
		*t = parsed
		return nil
	}
	var raw struct {
		Physical flexInt64  `json:"physical_ms"`
		Logical  flexUint64 `json:"logical"`
		NodeID   string     `json:"node_id"`
	}
	if err := json.Unmarshal(b, &raw); err != nil {
		return fmt.Errorf("%w: %v", ErrInvalidTimestamp, err)
	}
	*t = Timestamp{Physical: int64(raw.Physical), Logical: uint64(raw.Logical), NodeID: raw.NodeID}
	return t.Validate()
}
