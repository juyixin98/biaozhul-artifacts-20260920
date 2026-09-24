package tree

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

// canonicalNode is the deterministic JSON shape used for content hashing.
// Map iteration order in Go is randomized, so hashing d directly would not be
// stable; we sort node ids and serialise each node through ordered struct
// fields instead.
type canonicalNode struct {
	ID               string         `json:"id"`
	Kind             Kind           `json:"kind"`
	Children         []string       `json:"children,omitempty"`
	Action           string         `json:"action,omitempty"`
	Idempotent       bool           `json:"idempotent,omitempty"`
	Params           map[string]any `json:"params,omitempty"`
	TimeoutMS        int64          `json:"timeout_ms,omitempty"`
	SuccessThreshold int            `json:"success_threshold,omitempty"`
	FailureThreshold int            `json:"failure_threshold,omitempty"`
}

type canonicalDefinition struct {
	Root  string          `json:"root"`
	Nodes []canonicalNode `json:"nodes"`
}

// canonicalJSON renders the definition in a byte-stable form. Params maps are
// re-keyed via encoding/json with sorted keys (Go sorts map keys when
// marshalling), so two semantically equal definitions always agree byte for
// byte.
func (d *Definition) canonicalJSON() ([]byte, error) {
	ids := make([]string, 0, len(d.Nodes))
	for id := range d.Nodes {
		ids = append(ids, id)
	}
	// sort.Slice to avoid pulling in extra imports ordering.
	for i := 1; i < len(ids); i++ {
		for j := i; j > 0 && ids[j-1] > ids[j]; j-- {
			ids[j-1], ids[j] = ids[j], ids[j-1]
		}
	}
	cd := canonicalDefinition{Root: d.Root, Nodes: make([]canonicalNode, 0, len(ids))}
	for _, id := range ids {
		n := d.Nodes[id]
		cd.Nodes = append(cd.Nodes, canonicalNode{
			ID:               id,
			Kind:             n.Kind,
			Children:         n.Children,
			Action:           n.Action,
			Idempotent:       n.Idempotent,
			Params:           n.Params,
			TimeoutMS:        n.TimeoutMS,
			SuccessThreshold: n.SuccessThreshold,
			FailureThreshold: n.FailureThreshold,
		})
	}
	return json.Marshal(cd)
}

// ContentHash returns the hex SHA-256 of the canonical JSON encoding. SHA-256
// is a real cryptographic hash computed at publish time; identical
// definitions always map to the same version hash.
func (d *Definition) ContentHash() (string, error) {
	b, err := d.canonicalJSON()
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), nil
}
