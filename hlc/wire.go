package hlc

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// Timestamp is an HLC timestamp: a physical part (Unix milliseconds),
// a logical counter, and the node that minted it.
//
// Physical and Logical are 64-bit integers and are serialized as JSON
// numbers; Go decodes them losslessly. For clients built on IEEE-754 double
// (e.g. browser JavaScript), the exact value is also available in the
// "wire" field and via the hlc:// canonical text form.
type Timestamp struct {
	Physical int64  `json:"physical_ms"`
	Logical  uint64 `json:"logical"`
	NodeID   string `json:"node_id"`
}

// Compare returns -1, 0, or 1: timestamps are ordered lexicographically by
// (Physical, Logical). NodeID is not part of the HLC order; two timestamps
// equal on both parts are considered equal even if they name different nodes
// (such ties cannot occur for causally related events under the algorithm).
func (t Timestamp) Compare(o Timestamp) int {
	if t.Physical < o.Physical {
		return -1
	}
	if t.Physical > o.Physical {
		return 1
	}
	if t.Logical < o.Logical {
		return -1
	}
	if t.Logical > o.Logical {
		return 1
	}
	return 0
}

// Less reports whether t causally precedes o.
func (t Timestamp) Less(o Timestamp) bool { return t.Compare(o) < 0 }

// Equal reports identical physical and logical parts.
func (t Timestamp) Equal(o Timestamp) bool {
	return t.Physical == o.Physical && t.Logical == o.Logical
}

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

// ValidateNodeID enforces non-empty node ids without ':' or whitespace
// (':' delimits the canonical wire form, whitespace breaks log lines).
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

// ---- Canonical text form: hlc://node/<physical_ms>:<logical> ----

// WirePrefix prefixes the canonical text form.
const WirePrefix = "hlc://"

// Wire returns the canonical text form "hlc://node/<physical>:<logical>".
func (t Timestamp) Wire() string {
	return fmt.Sprintf("%s%s/%d:%d", WirePrefix, t.NodeID, t.Physical, t.Logical)
}

// ParseWire parses the canonical text form.
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
	log, err := strconv.ParseUint(logS, 10, 64)
	if err != nil {
		return Timestamp{}, fmt.Errorf("%w: bad logical: %v", ErrInvalidTimestamp, err)
	}
	t := Timestamp{Physical: phys, Logical: log, NodeID: node}
	if err := t.Validate(); err != nil {
		return Timestamp{}, err
	}
	return t, nil
}

// ---- JSON: numbers are emitted losslessly; a quoted numeric string is also
// accepted on input (flexMillis/flexLogical), for clients that must transport
// the value through IEEE-754 doubles. ----

type flexMillis int64

func (f *flexMillis) UnmarshalJSON(b []byte) error {
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
		*f = flexMillis(v)
		return nil
	}
	v, err := strconv.ParseInt(string(b), 10, 64)
	if err != nil {
		return fmt.Errorf("%w: bad physical_ms: %v", ErrInvalidTimestamp, err)
	}
	*f = flexMillis(v)
	return nil
}

type flexLogical uint64

func (f *flexLogical) UnmarshalJSON(b []byte) error {
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
		*f = flexLogical(v)
		return nil
	}
	v, err := strconv.ParseUint(string(b), 10, 64)
	if err != nil {
		return fmt.Errorf("%w: bad logical: %v", ErrInvalidTimestamp, err)
	}
	*f = flexLogical(v)
	return nil
}

// MarshalJSON emits the object plus the exact canonical "wire" form.
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
//	{"physical_ms":"1700","logical":"3","node_id":"a"}   (quoted numbers)
//	"hlc://a/1700:3"                                       (canonical text)
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
		Physical flexMillis  `json:"physical_ms"`
		Logical  flexLogical `json:"logical"`
		NodeID   string      `json:"node_id"`
	}
	if err := json.Unmarshal(b, &raw); err != nil {
		return fmt.Errorf("%w: %v", ErrInvalidTimestamp, err)
	}
	*t = Timestamp{Physical: int64(raw.Physical), Logical: uint64(raw.Logical), NodeID: raw.NodeID}
	return t.Validate()
}
