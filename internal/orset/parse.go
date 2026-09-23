package orset

import (
	"fmt"
	"strconv"
	"strings"
)

// parseTag parses "origin:seq". Origin must be non-empty; this is enforced for
// tags minted inside the simulator.
func parseTag(s string) (Tag, error) {
	i := strings.LastIndexByte(s, ':')
	if i <= 0 {
		return Tag{}, fmt.Errorf("invalid tag %q: want origin:seq", s)
	}
	seq, err := strconv.ParseUint(s[i+1:], 10, 64)
	if err != nil {
		return Tag{}, fmt.Errorf("invalid tag seq in %q: %w", s, err)
	}
	return Tag{Origin: s[:i], Seq: seq}, nil
}
