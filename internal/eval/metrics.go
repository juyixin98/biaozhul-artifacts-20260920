package eval

import (
	"fmt"
	"sort"
)

// Prediction pairs a ground-truth label with the assigned template id.
type Prediction struct {
	Label      string
	TemplateID int64
}

// Metrics holds clustering quality numbers.
type Metrics struct {
	Lines          int
	Clusters       int
	DistinctLabels int
	// Pairwise counts and derived measures.
	TP, FP, FN, TN int64
	Precision      float64 // of pairs clustered together, how many truly share a label
	Recall         float64 // of pairs truly sharing a label, how many are clustered together
	F1             float64
	// Weighted cluster purity: majority-label fraction, weighted by cluster
	// size (== pairwise precision, reported separately for clarity).
	Purity float64
}

func (m Metrics) String() string {
	return fmt.Sprintf(
		"lines=%d clusters=%d labels=%d precision=%.4f recall=%.4f f1=%.4f purity=%.4f (tp=%d fp=%d fn=%d tn=%d)",
		m.Lines, m.Clusters, m.DistinctLabels, m.Precision, m.Recall, m.F1, m.Purity,
		m.TP, m.FP, m.FN, m.TN)
}

// Compute builds pairwise precision/recall/F1 and weighted purity.
// Pairwise definitions over all unordered line pairs (i<j):
//
//	TP: same predicted cluster and same label
//	FP: same predicted cluster, different label
//	FN: different predicted clusters, same label
//	TN: different cluster and different label
func Compute(preds []Prediction) Metrics {
	m := Metrics{Lines: len(preds)}

	// Aggregate counts per (cluster, label) cell, per cluster, per label.
	type cl struct {
		c int64
		l string
	}
	cells := map[cl]int{}
	clusterSize := map[int64]int{}
	labelSize := map[string]int{}
	clusters := map[int64]struct{}{}
	labels := map[string]struct{}{}
	for _, p := range preds {
		cells[cl{p.TemplateID, p.Label}]++
		clusterSize[p.TemplateID]++
		labelSize[p.Label]++
		clusters[p.TemplateID] = struct{}{}
		labels[p.Label] = struct{}{}
	}
	m.Clusters = len(clusters)
	m.DistinctLabels = len(labels)

	// TP = sum over cells of choose(n,2).
	for _, n := range cells {
		m.TP += int64(n) * int64(n-1) / 2
	}
	// choose(clusterSize,2) = TP + FP.
	var sameClusterPairs int64
	for _, n := range clusterSize {
		sameClusterPairs += int64(n) * int64(n-1) / 2
	}
	m.FP = sameClusterPairs - m.TP
	// choose(labelSize,2) = TP + FN.
	var sameLabelPairs int64
	for _, n := range labelSize {
		sameLabelPairs += int64(n) * int64(n-1) / 2
	}
	m.FN = sameLabelPairs - m.TP
	m.TN = int64(len(preds))*int64(len(preds)-1)/2 - m.TP - m.FP - m.FN
	if sameClusterPairs > 0 {
		m.Precision = float64(m.TP) / float64(sameClusterPairs)
	}
	if sameLabelPairs > 0 {
		m.Recall = float64(m.TP) / float64(sameLabelPairs)
	}
	if m.Precision+m.Recall > 0 {
		m.F1 = 2 * m.Precision * m.Recall / (m.Precision + m.Recall)
	}

	// Weighted purity: per-cluster majority-label fraction.
	var weighted float64
	cids := make([]int64, 0, len(clusters))
	for c := range clusters {
		cids = append(cids, c)
	}
	sort.Slice(cids, func(i, j int) bool { return cids[i] < cids[j] })
	for _, c := range cids {
		maj := 0
		for _, l := range sortedLabels(labels) {
			if n := cells[cl{c, l}]; n > maj {
				maj = n
			}
		}
		weighted += float64(maj)
	}
	if len(preds) > 0 {
		m.Purity = weighted / float64(len(preds))
	}
	return m
}

func sortedLabels(labels map[string]struct{}) []string {
	out := make([]string, 0, len(labels))
	for l := range labels {
		out = append(out, l)
	}
	sort.Strings(out)
	return out
}
