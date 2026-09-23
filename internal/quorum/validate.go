package quorum

import (
	"fmt"
	"sort"
)

// validated is a normalised configuration: nodes sorted by ID, domains
// compacted to sorted unique names, domainOf mapping node index -> domain
// index.
type validated struct {
	cfg      Config
	names    []string // node IDs by index (sorted)
	weights  []int
	domainOf []int
	domains  []string
	warnings []Warning
}

// Validate checks a configuration and returns its normalised form and any
// non-fatal warnings. It returns errors for invalid configurations; a config
// with errors must not be analysed (it cannot define meaningful quorums).
func Validate(cfg Config) (*validated, []string) {
	var errs []string

	if len(cfg.Nodes) == 0 {
		errs = append(errs, "nodes: at least one node is required")
	}
	if len(cfg.Nodes) > MaxNodes {
		errs = append(errs, fmt.Sprintf("nodes: exhaustive analysis supports at most %d nodes, got %d", MaxNodes, len(cfg.Nodes)))
	}
	if cfg.ReadQuorum <= 0 {
		errs = append(errs, fmt.Sprintf("read_quorum: must be a positive integer, got %d", cfg.ReadQuorum))
	}
	if cfg.WriteQuorum <= 0 {
		errs = append(errs, fmt.Sprintf("write_quorum: must be a positive integer, got %d", cfg.WriteQuorum))
	}
	if cfg.TolerateDomains < 0 {
		errs = append(errs, fmt.Sprintf("tolerate_domains: must be non-negative, got %d", cfg.TolerateDomains))
	}

	seen := map[string]bool{}
	var warnings []Warning
	var zeroWeight []string
	missingDomain := 0
	total := 0

	for i, n := range cfg.Nodes {
		if n.ID == "" {
			errs = append(errs, fmt.Sprintf("nodes[%d]: id must be non-empty", i))
		} else if seen[n.ID] {
			errs = append(errs, fmt.Sprintf("nodes[%d]: duplicate node id %q", i, n.ID))
		} else {
			seen[n.ID] = true
		}
		if n.Weight < 0 {
			errs = append(errs, fmt.Sprintf("nodes[%d] (%s): weight must be non-negative, got %d", i, label(n.ID), n.Weight))
		}
		if n.Weight == 0 {
			zeroWeight = append(zeroWeight, n.ID)
		}
		if n.Domain == "" {
			missingDomain++
		}
		total += n.Weight
	}

	if cfg.ReadQuorum > 0 && cfg.ReadQuorum > total {
		errs = append(errs, fmt.Sprintf("read_quorum %d exceeds total node weight %d: no read quorum can ever form", cfg.ReadQuorum, total))
	}
	if cfg.WriteQuorum > 0 && cfg.WriteQuorum > total {
		errs = append(errs, fmt.Sprintf("write_quorum %d exceeds total node weight %d: no write quorum can ever form", cfg.WriteQuorum, total))
	}

	if len(errs) > 0 {
		return nil, errs
	}

	sort.Strings(zeroWeight)
	if len(zeroWeight) > 0 {
		warnings = append(warnings, Warning{
			Code:    "zero_weight_nodes",
			Message: fmt.Sprintf("%d node(s) have weight 0 and can never contribute to a quorum: %v", len(zeroWeight), zeroWeight),
		})
	}
	if missingDomain > 0 {
		warnings = append(warnings, Warning{
			Code:    "missing_domain",
			Message: fmt.Sprintf("%d node(s) have no explicit failure domain; placed in \"default\"", missingDomain),
		})
	}

	// Normalise: stable copy sorted by ID.
	order := make([]int, len(cfg.Nodes))
	for i := range order {
		order[i] = i
	}
	sort.Slice(order, func(a, b int) bool { return cfg.Nodes[order[a]].ID < cfg.Nodes[order[b]].ID })

	v := &validated{cfg: cfg, warnings: warnings}
	v.names = make([]string, len(order))
	v.weights = make([]int, len(order))
	domName := map[string]int{}
	for newIdx, oldIdx := range order {
		n := cfg.Nodes[oldIdx]
		v.names[newIdx] = n.ID
		v.weights[newIdx] = n.Weight
		d := n.Domain
		if d == "" {
			d = "default"
		}
		di, ok := domName[d]
		if !ok {
			di = len(v.domains)
			domName[d] = di
			v.domains = append(v.domains, d)
		}
		v.domainOf = append(v.domainOf, di)
	}
	sort.Strings(v.domains)
	// Rebuild domain indices after sorting domain names.
	index := map[string]int{}
	for i, d := range v.domains {
		index[d] = i
	}
	for i, oldIdx := range order {
		d := cfg.Nodes[oldIdx].Domain
		if d == "" {
			d = "default"
		}
		v.domainOf[i] = index[d]
	}

	if cfg.TolerateDomains > len(v.domains) {
		v.warnings = append(v.warnings, Warning{
			Code:    "tolerance_exceeds_domains",
			Message: fmt.Sprintf("tolerate_domains %d exceeds number of domains %d; capped", cfg.TolerateDomains, len(v.domains)),
		})
	}

	return v, nil
}

func label(id string) string {
	if id == "" {
		return "<empty>"
	}
	return id
}
