package eval

import (
	"strings"
	"testing"

	"logcluster/internal/cluster"
)

func TestComputePerfectClustering(t *testing.T) {
	// Two labels, each split across its own cluster -> perfect.
	preds := []Prediction{
		{"a", 1}, {"a", 1}, {"a", 1},
		{"b", 2}, {"b", 2},
	}
	m := Compute(preds)
	if m.Precision != 1 || m.Recall != 1 || m.F1 != 1 || m.Purity != 1 {
		t.Fatalf("expected perfect metrics, got %s", m)
	}
	if m.TP != 4 { // choose(3,2) + choose(2,2)
		t.Fatalf("TP = %d, want 4", m.TP)
	}
}

func TestComputeMergeAndSplit(t *testing.T) {
	// Cluster 1 mixes labels a,b (FP), label a is split across 1 and 2 (FN).
	preds := []Prediction{
		{"a", 1}, {"a", 1}, {"b", 1},
		{"a", 2},
	}
	m := Compute(preds)
	// Pairs: (a1,a2)=TP, (a1,b)=FP, (a2,b)=FP, (a1,a3)=FN, (a2,a3)=FN, (b,a3)=TN
	if m.TP != 1 || m.FP != 2 || m.FN != 2 || m.TN != 1 {
		t.Fatalf("counts wrong: tp=%d fp=%d fn=%d tn=%d", m.TP, m.FP, m.FN, m.TN)
	}
	if m.Precision != 1.0/3.0 {
		t.Fatalf("precision = %v, want 1/3", m.Precision)
	}
	if m.Recall != 1.0/3.0 {
		t.Fatalf("recall = %v, want 1/3", m.Recall)
	}
	// Purity: cluster1 majority 2/3, cluster2 1/1 -> weighted 3/4.
	if m.Purity != 0.75 {
		t.Fatalf("purity = %v, want 0.75", m.Purity)
	}
}

func TestDatasetDeterministic(t *testing.T) {
	d1 := Build(42, 10, 30)
	d2 := Build(42, 10, 30)
	if len(d1.Mixed) != len(d2.Mixed) || len(d1.Bulk) != len(d2.Bulk) {
		t.Fatal("dataset sizes differ")
	}
	for i := range d1.Mixed {
		if d1.Mixed[i] != d2.Mixed[i] {
			t.Fatalf("Mixed[%d] non-deterministic: %q vs %q", i, d1.Mixed[i], d2.Mixed[i])
		}
	}
}

func TestEndToEndQuality(t *testing.T) {
	ds := Build(42, 100, 30)
	cl := cluster.New(cluster.Config{})
	preds := make([]Prediction, 0, len(ds.Mixed))
	for _, ll := range ds.Mixed {
		a, err := cl.Ingest(ll.Line)
		if err != nil {
			t.Fatalf("ingest error: %v", err)
		}
		preds = append(preds, Prediction{ll.Label, a.TemplateID})
	}
	m := Compute(preds)
	t.Log(m)

	// Keyword-different error pairs must remain distinct: precision must be 1.
	if m.Precision != 1.0 {
		t.Errorf("precision = %.4f, want 1.0 (keyword errors merged?)", m.Precision)
	}
	// Every labeled template should be captured in exactly one cluster.
	if m.Recall != 1.0 {
		t.Errorf("recall = %.4f, want 1.0 (fragmentation?)", m.Recall)
	}
	if m.Clusters != m.DistinctLabels {
		t.Errorf("clusters=%d labels=%d (expected equal)", m.Clusters, m.DistinctLabels)
	}

	// Super-long lines must have been handled and clustered together.
	traceCount := 0
	for _, ll := range ds.Mixed {
		if ll.Label == "trace.long" {
			traceCount++
		}
	}
	if traceCount != 2 {
		t.Fatalf("expected 2 long lines in dataset, got %d", traceCount)
	}
}

func TestEndToEndEviction(t *testing.T) {
	ds := Build(42, 100, 30)
	const cap = 12 // 10 hot templates + room for 2 one-off cold templates
	cl := cluster.New(cluster.Config{MaxTemplates: cap})
	for _, ll := range ds.Bulk {
		if _, err := cl.Ingest(ll.Line); err != nil {
			t.Fatal(err)
		}
	}
	st := cl.Stats()
	if st.Templates != cap {
		t.Errorf("live templates = %d, want %d", st.Templates, cap)
	}
	// 20 distinct cold templates arrive while the 10 hot templates are
	// revisited every round. Only 2 cold slots exist, so the other 18 are
	// evicted; no hot template is ever evicted (LRU correctness).
	const coldTemplates = 20
	wantEvictions := coldTemplates - (cap - 10)
	if st.Evictions != wantEvictions {
		t.Errorf("evictions = %d, want %d", st.Evictions, wantEvictions)
	}
	for _, e := range cl.Evictions() {
		if !strings.Contains(e.Pattern, " cold ") {
			t.Errorf("non-cold template evicted: %q", e.Pattern)
		}
	}
}
