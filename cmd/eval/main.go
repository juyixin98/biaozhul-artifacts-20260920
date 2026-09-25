// Command eval generates the labeled synthetic corpus, runs it through the
// clustering engine, and reports purity/recall. Exit status is non-zero when
// thresholds are not met, so it doubles as an acceptance gate.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"

	"logcluster/internal/engine"
	"logcluster/internal/eval"
	"logcluster/internal/synth"
)

func main() {
	minPurity := flag.Float64("min-purity", 1.0, "minimum accepted purity")
	minRecall := flag.Float64("min-recall", 1.0, "minimum accepted recall")
	asJSON := flag.Bool("json", false, "emit machine-readable JSON report")
	flag.Parse()

	ds := synth.Build()
	eng := engine.New(engine.DefaultConfig(), eval.NewClock())
	rep := eval.Run(eng, ds)
	rep.Validate()

	if *asJSON {
		out := struct {
			*eval.Report
			MinPurity float64 `json:"threshold_purity"`
			MinRecall float64 `json:"threshold_recall"`
			Passed    bool    `json:"passed"`
		}{
			Report:    rep,
			MinPurity: *minPurity,
			MinRecall: *minRecall,
			Passed:    len(rep.Failures) == 0 && rep.Purity >= *minPurity && rep.Recall >= *minRecall,
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(out); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
	} else {
		printText(rep)
	}

	if rep.Purity < *minPurity || rep.Recall < *minRecall || len(rep.Failures) > 0 {
		if !*asJSON {
			fmt.Println("\nRESULT: FAIL")
			for _, f := range rep.Failures {
				fmt.Println("  - " + f)
			}
		}
		os.Exit(1)
	}
	if !*asJSON {
		fmt.Println("\nRESULT: PASS")
	}
}

func printText(r *eval.Report) {
	fmt.Println("=== Log template clustering: labeled evaluation ===")
	fmt.Printf("lines=%d  ground-truth groups=%d  produced clusters=%d  truncated lines=%d\n",
		r.TotalLines, r.UniqueGroups, r.Clusters, r.TruncatedLines)
	fmt.Printf("purity=%.4f  recall=%.4f  f1=%.4f  min-group-recall=%.4f\n",
		r.Purity, r.Recall, r.F1, r.MinGroupRecall)

	fmt.Println("\n-- Per ground-truth group (recall) --")
	for _, g := range r.GroupRows {
		// cluster shares deterministically ordered
		ids := make([]int, 0, len(g.ClusterShare))
		for cid := range g.ClusterShare {
			ids = append(ids, cid)
		}
		sort.Ints(ids)
		parts := make([]string, 0, len(ids))
		for _, cid := range ids {
			parts = append(parts, fmt.Sprintf("c%d=%d", cid, g.ClusterShare[cid]))
		}
		main := "-"
		if g.MainCluster >= 0 {
			main = fmt.Sprintf("c%d", g.MainCluster)
		}
		fmt.Printf("  %-16s lines=%-3d main=%-4s recall=%.3f  [%s]\n",
			g.Group, g.Lines, main, g.Recall, strings.Join(parts, " "))
	}

	fmt.Println("\n-- Per produced cluster (purity) --")
	for _, c := range r.ClusterRows {
		tpl := c.Template
		if len(tpl) > 110 {
			tpl = tpl[:107] + "..."
		}
		fmt.Printf("  c%-3d v%d n=%-3d purity=%.3f main=%-16s %s\n",
			c.ClusterID, c.Version, c.Count, c.Purity, c.MainGroup, tpl)
	}
}
