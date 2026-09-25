// Package eval runs the labeled synthetic corpus through the engine and
// computes clustering quality:
//
//   - Purity  = sum_c max_g |cluster c ∩ group g| / N. 1.0 means every cluster
//     is dominated by a single ground-truth group.
//   - Recall  = weighted inverse purity: sum_g max_c |cluster c ∩ group g| / N.
//     1.0 means every group is captured by a single cluster (not fragmented).
//
// Both must be high together: merging all lines into one cluster gives purity
// 1/N but recall 1.0; one cluster per line inverts the trade-off.
package eval

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"logcluster/internal/engine"
	"logcluster/internal/synth"
)

// Clock is a deterministic clock: each ingest advances one millisecond,
// keeping eviction ordering reproducible.
type Clock struct {
	t time.Time
}

// NewClock returns a clock pinned to a fixed epoch.
func NewClock() *Clock {
	return &Clock{t: time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)}
}

// Tick advances and returns the new timestamp.
func (c *Clock) Now() time.Time {
	c.t = c.t.Add(time.Millisecond)
	return c.t
}

// GroupRow reports one ground-truth group's distribution across clusters.
type GroupRow struct {
	Group        string      `json:"group"`
	Lines        int         `json:"lines"`
	MainCluster  int         `json:"main_cluster"`
	ClusterShare map[int]int `json:"-"`
	Recall       float64     `json:"recall"`
}

// ClusterRow reports one cluster's composition.
type ClusterRow struct {
	ClusterID int            `json:"cluster_id"`
	Template  string         `json:"template"`
	Version   int            `json:"version"`
	Count     int64          `json:"count"`
	MainGroup string         `json:"main_group"`
	Share     map[string]int `json:"-"`
	Purity    float64        `json:"purity"`
}

// Report is the full evaluation result.
type Report struct {
	TotalLines       int            `json:"total_lines"`
	UniqueGroups     int            `json:"unique_groups"`
	Clusters         int            `json:"clusters"`
	TruncatedLines   int64          `json:"truncated_lines"`
	Purity           float64        `json:"purity"`
	Recall           float64        `json:"recall"`
	F1               float64        `json:"f1"`
	MinGroupRecall   float64        `json:"min_group_recall"`
	GroupRows        []GroupRow     `json:"group_rows"`
	ClusterRows      []ClusterRow   `json:"cluster_rows"`
	ExpectedClusters map[string]int `json:"expected_clusters,omitempty"`
	Failures         []string       `json:"failures"`
}

// Run ingests the dataset and scores the resulting clustering.
func Run(eng *engine.Engine, ds synth.Dataset) *Report {
	type assignment struct {
		clusterID int
		group     string
	}
	assignments := make([]assignment, 0, len(ds.Samples))
	for _, s := range ds.Samples {
		ev, err := eng.Ingest(s.Line)
		if err != nil {
			// synth never emits empty lines; an error here is a real failure.
			assignments = append(assignments, assignment{clusterID: -1, group: s.Group})
			continue
		}
		assignments = append(assignments, assignment{clusterID: ev.ClusterID, group: s.Group})
	}

	groupIDs := make([]string, 0, len(ds.Groups))
	groupCount := map[string]int{}
	for _, g := range ds.Groups {
		groupIDs = append(groupIDs, g.ID)
		groupCount[g.ID] = g.Count
	}
	sort.Strings(groupIDs)

	clusterIDsSet := map[int]struct{}{}
	for _, a := range assignments {
		if a.clusterID >= 0 {
			clusterIDsSet[a.clusterID] = struct{}{}
		}
	}
	clusterIDs := make([]int, 0, len(clusterIDsSet))
	for id := range clusterIDsSet {
		clusterIDs = append(clusterIDs, id)
	}
	sort.Ints(clusterIDs)

	// contingency[cluster][group] = count
	contingency := map[int]map[string]int{}
	for _, a := range assignments {
		if contingency[a.clusterID] == nil {
			contingency[a.clusterID] = map[string]int{}
		}
		contingency[a.clusterID][a.group]++
	}

	n := len(assignments)
	var puritySum, recallSum int
	rows := make([]ClusterRow, 0, len(clusterIDs))
	for _, cid := range clusterIDs {
		share := contingency[cid]
		var cTotal int
		var bestGroup string
		best := 0
		for _, g := range groupIDs {
			cTotal += share[g]
			if share[g] > best {
				best = share[g]
				bestGroup = g
			}
		}
		puritySum += best
		var tpl string
		var version int
		if info, ok := eng.Cluster(cid); ok {
			tpl = info.Template
			version = info.Version
		}
		rows = append(rows, ClusterRow{
			ClusterID: cid,
			Template:  tpl,
			Version:   version,
			Count:     int64(cTotal),
			MainGroup: bestGroup,
			Share:     share,
			Purity:    float64(best) / float64(cTotal),
		})
	}

	gRows := make([]GroupRow, 0, len(groupIDs))
	minRecall := 1.0
	for _, g := range groupIDs {
		total := 0
		bestC, bestN := -1, 0
		share := map[int]int{}
		for _, cid := range clusterIDs {
			v := contingency[cid][g]
			if v > 0 {
				share[cid] = v
				total += v
				if v > bestN {
					bestN, bestC = v, cid
				}
			}
		}
		recallSum += bestN
		rc := 0.0
		if total > 0 {
			rc = float64(bestN) / float64(total)
		}
		if rc < minRecall {
			minRecall = rc
		}
		gRows = append(gRows, GroupRow{
			Group:        g,
			Lines:        groupCount[g],
			MainCluster:  bestC,
			ClusterShare: share,
			Recall:       rc,
		})
	}

	purity := float64(puritySum) / float64(n)
	recall := float64(recallSum) / float64(n)
	f1 := 0.0
	if purity+recall > 0 {
		f1 = 2 * purity * recall / (purity + recall)
	}

	stats := eng.Stats()
	rep := &Report{
		TotalLines:     n,
		UniqueGroups:   len(groupIDs),
		Clusters:       len(clusterIDs),
		TruncatedLines: stats.TruncatedLines,
		Purity:         purity,
		Recall:         recall,
		F1:             f1,
		MinGroupRecall: minRecall,
		GroupRows:      gRows,
		ClusterRows:    rows,
	}
	return rep
}

// Validate adds human-readable failures when quality or structural checks fail.
func (r *Report) Validate() {
	const tol = 1.0 // corpus is designed for perfect scores
	if r.Purity < tol {
		r.Failures = append(r.Failures, fmt.Sprintf("purity %.4f < %.2f (some cluster mixes groups)", r.Purity, tol))
	}
	if r.Recall < tol {
		r.Failures = append(r.Failures, fmt.Sprintf("recall %.4f < %.2f (some group is fragmented)", r.Recall, tol))
	}
	for _, g := range r.GroupRows {
		if g.Recall < tol {
			parts := make([]string, 0, len(g.ClusterShare))
			for cid, n := range g.ClusterShare {
				parts = append(parts, fmt.Sprintf("cluster %d:%d", cid, n))
			}
			sort.Strings(parts)
			r.Failures = append(r.Failures,
				fmt.Sprintf("group %s fragmented: %s", g.Group, strings.Join(parts, ", ")))
		}
	}
	for _, c := range r.ClusterRows {
		if c.Purity < tol {
			parts := make([]string, 0, len(c.Share))
			for g, n := range c.Share {
				parts = append(parts, fmt.Sprintf("%s:%d", g, n))
			}
			sort.Strings(parts)
			r.Failures = append(r.Failures,
				fmt.Sprintf("cluster %d mixes groups: %s", c.ClusterID, strings.Join(parts, ", ")))
		}
	}
}
