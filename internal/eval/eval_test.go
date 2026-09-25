package eval

import (
	"testing"

	"logcluster/internal/engine"
	"logcluster/internal/synth"
)

func TestLabeledCorpusScores(t *testing.T) {
	ds := synth.Build()
	if len(ds.Samples) != 208 {
		t.Fatalf("corpus size = %d, want 208", len(ds.Samples))
	}
	eng := engine.New(engine.DefaultConfig(), NewClock())
	rep := Run(eng, ds)
	rep.Validate()

	if rep.Purity != 1.0 {
		t.Errorf("purity = %.4f, want 1.0", rep.Purity)
	}
	if rep.Recall != 1.0 {
		t.Errorf("recall = %.4f, want 1.0", rep.Recall)
	}
	if rep.F1 != 1.0 {
		t.Errorf("f1 = %.4f, want 1.0", rep.F1)
	}
	if rep.Clusters != len(ds.Groups) {
		t.Errorf("clusters = %d, want %d ground-truth groups", rep.Clusters, len(ds.Groups))
	}
	if rep.TruncatedLines != 6 {
		t.Errorf("truncated lines = %d, want 6", rep.TruncatedLines)
	}
	if len(rep.Failures) != 0 {
		t.Errorf("unexpected failures: %v", rep.Failures)
	}
}

func TestWordParamClusterHasTwoVersions(t *testing.T) {
	ds := synth.Build()
	eng := engine.New(engine.DefaultConfig(), NewClock())
	Run(eng, ds)

	var found bool
	for _, ti := range eng.Templates() {
		if ti.Version == 2 && len(ti.Versions) == 2 && ti.Count > 0 {
			found = true
		}
	}
	if !found {
		t.Error("expected at least one cluster at version 2 with history and lines")
	}
}
