package tree

import (
	"encoding/json"
	"fmt"
)

// ParseDefinition decodes JSON into a Definition and normalises it:
// omitted children become nil slices and embedded IDs (if present) must match
// map keys. It does not validate; call Validate separately.
func ParseDefinition(raw []byte) (*Definition, error) {
	var d Definition
	if err := json.Unmarshal(raw, &d); err != nil {
		return nil, fmt.Errorf("invalid JSON: %w", err)
	}
	if d.Nodes == nil {
		d.Nodes = map[string]Node{}
	}
	return &d, nil
}
