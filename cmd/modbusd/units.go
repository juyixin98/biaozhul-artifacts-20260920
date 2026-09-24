package main

import (
	"fmt"
	"strconv"
	"strings"
)

// parseUnits parses "1,2,3" into a byte set of Unit IDs.
func parseUnits(s string) ([]byte, error) {
	var out []byte
	seen := map[int]bool{}
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		n, err := strconv.Atoi(part)
		if err != nil {
			return nil, fmt.Errorf("bad unit id %q: %w", part, err)
		}
		if n < 0 || n > 255 {
			return nil, fmt.Errorf("unit id %d out of byte range", n)
		}
		if !seen[n] {
			seen[n] = true
			out = append(out, byte(n))
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("at least one unit id required")
	}
	return out, nil
}
