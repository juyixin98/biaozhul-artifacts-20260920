// Command eval runs the deterministic labeled evaluation: it feeds the
// synthetic dataset through the clusterer and prints purity/recall metrics,
// plus rare-pattern, long-line and capacity-eviction scenario results.
package main

import (
	"flag"
	"fmt"
	"sort"

	"logcluster/internal/cluster"
	"logcluster/internal/eval"
)

func main() {
	seed := flag.Int64("seed", 42, "dataset seed (fixed for determinism)")
	perTemplate := flag.Int("per-template", 100, "occurrences per base template")
	bulkRounds := flag.Int("bulk-rounds", 30, "rounds in the eviction stream (10 hot templates + one cold per round)")
	bulkCapacity := flag.Int("bulk-capacity", 12, "template capacity for the eviction scenario (10 hot + 2 cold slots)")
	flag.Parse()

	ds := eval.Build(*seed, *perTemplate, *bulkRounds)

	fmt.Println("== scenario 1: mixed stream, default capacity ==")
	cl := cluster.New(cluster.Config{})
	preds := make([]eval.Prediction, 0, len(ds.Mixed))
	for _, ll := range ds.Mixed {
		a, err := cl.Ingest(ll.Line)
		if err != nil {
			fmt.Printf("ingest error for %q: %v\n", ll.Line, err)
			continue
		}
		preds = append(preds, eval.Prediction{Label: ll.Label, TemplateID: a.TemplateID})
	}
	m := eval.Compute(preds)
	fmt.Println(m)
	printLabelMapping(cl, preds)

	fmt.Println()
	fmt.Println("== scenario 2: capacity eviction ==")
	cl2 := cluster.New(cluster.Config{MaxTemplates: *bulkCapacity})
	preds2 := make([]eval.Prediction, 0, len(ds.Bulk))
	for _, ll := range ds.Bulk {
		a, err := cl2.Ingest(ll.Line)
		if err != nil {
			fmt.Printf("ingest error for %q: %v\n", ll.Line, err)
			continue
		}
		preds2 = append(preds2, eval.Prediction{Label: ll.Label, TemplateID: a.TemplateID})
	}
	st := cl2.Stats()
	fmt.Printf("capacity=%d ingested=%d live_templates=%d evictions=%d\n",
		st.Capacity, st.Ingested, st.Templates, st.Evictions)
	fmt.Println(eval.Compute(preds2))
	fmt.Println("evicted template ids:", evictedIDs(cl2))
}

// printLabelMapping shows, per true label, which template ids captured it —
// the human-readable check that keyword-different errors stayed separate.
func printLabelMapping(cl *cluster.Clusterer, preds []eval.Prediction) {
	byLabel := map[string]map[int64]int{}
	for _, p := range preds {
		if byLabel[p.Label] == nil {
			byLabel[p.Label] = map[int64]int{}
		}
		byLabel[p.Label][p.TemplateID]++
	}
	labels := make([]string, 0, len(byLabel))
	for l := range byLabel {
		labels = append(labels, l)
	}
	sort.Strings(labels)
	fmt.Println("label -> templates:")
	for _, l := range labels {
		ids := make([]int64, 0, len(byLabel[l]))
		for id := range byLabel[l] {
			ids = append(ids, id)
		}
		sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
		for _, id := range ids {
			t, ok := cl.Get(id)
			pat, ver := "?", 0
			if ok {
				pat, ver = t.Pattern, t.Version
			}
			fmt.Printf("  %-14s -> #%d (v%d, n=%d) %s\n", l, id, ver, byLabel[l][id], pat)
		}
	}
}

func evictedIDs(cl *cluster.Clusterer) []int64 {
	var out []int64
	for _, e := range cl.Evictions() {
		out = append(out, e.TemplateID)
	}
	return out
}
