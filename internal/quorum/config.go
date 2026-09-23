// Package quorum implements validation and safety checking for weighted
// quorum configurations under a failure-domain model.
package quorum

import (
	"fmt"
	"sort"
)

// Node is a single replica with an integer weight and a failure-domain tag.
// Nodes in the same domain are assumed to fail together (e.g. a rack or AZ).
type Node struct {
	ID     string `json:"id"`
	Weight int    `json:"weight"`
	Domain string `json:"domain"`
}

// Config describes a weighted quorum system.
type Config struct {
	Nodes          []Node `json:"nodes"`
	ReadThreshold  int    `json:"read_threshold"`
	WriteThreshold int    `json:"write_threshold"`
	// MaxFailedDomains is the maximum number of failure domains that may be
	// unavailable simultaneously. The checker enumerates every subset of
	// domains up to this size.
	MaxFailedDomains int `json:"max_failed_domains"`
}

// MaxNodes bounds the exhaustive enumeration (2^MaxNodes subsets per scenario).
const MaxNodes = 20

// Validate checks structural correctness and returns non-fatal warnings.
// A node with an empty Domain is treated as its own failure domain.
func (c *Config) Validate() ([]string, error) {
	var warnings []string
	if len(c.Nodes) == 0 {
		return nil, fmt.Errorf("config must contain at least one node")
	}
	if len(c.Nodes) > MaxNodes {
		return nil, fmt.Errorf("too many nodes: %d (max %d for exhaustive checking)", len(c.Nodes), MaxNodes)
	}
	seen := make(map[string]bool, len(c.Nodes))
	for _, n := range c.Nodes {
		if n.ID == "" {
			return nil, fmt.Errorf("node with empty id")
		}
		if seen[n.ID] {
			return nil, fmt.Errorf("duplicate node id %q", n.ID)
		}
		seen[n.ID] = true
		if n.Weight < 0 {
			return nil, fmt.Errorf("node %q has negative weight %d", n.ID, n.Weight)
		}
		if n.Weight == 0 {
			warnings = append(warnings, fmt.Sprintf("node %q has zero weight: it can join quorums but contributes nothing", n.ID))
		}
	}
	if c.ReadThreshold <= 0 {
		return nil, fmt.Errorf("read_threshold must be positive, got %d", c.ReadThreshold)
	}
	if c.WriteThreshold <= 0 {
		return nil, fmt.Errorf("write_threshold must be positive, got %d", c.WriteThreshold)
	}
	if c.MaxFailedDomains < 0 {
		return nil, fmt.Errorf("max_failed_domains must be >= 0, got %d", c.MaxFailedDomains)
	}
	return warnings, nil
}

// SortedNodes returns a copy of the node list sorted by ID. All enumeration
// and reporting goes through this so results are deterministic.
func (c *Config) SortedNodes() []Node {
	out := make([]Node, len(c.Nodes))
	copy(out, c.Nodes)
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// TotalWeight sums all node weights.
func (c *Config) TotalWeight() int {
	t := 0
	for _, n := range c.Nodes {
		t += n.Weight
	}
	return t
}

// Domains returns the sorted, de-duplicated list of failure domains.
// Empty domains are replaced by the node ID (each such node is its own domain).
func (c *Config) Domains() []string {
	set := map[string]bool{}
	for _, n := range c.Nodes {
		d := n.Domain
		if d == "" {
			d = n.ID
		}
		set[d] = true
	}
	out := make([]string, 0, len(set))
	for d := range set {
		out = append(out, d)
	}
	sort.Strings(out)
	return out
}

// domainOf mirrors Domains' defaulting for a single node.
func domainOf(n Node) string {
	if n.Domain == "" {
		return n.ID
	}
	return n.Domain
}
