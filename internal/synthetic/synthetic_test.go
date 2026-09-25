package synthetic

import (
	"reflect"
	"testing"
)

func TestGenerateDeterministic(t *testing.T) {
	opts := Options{
		Metric: "cpu", Labels: map[string]string{"host": "a"},
		Start: 0, End: 100, PeriodSec: 10,
		Amplitude: 5, Offset: 50, Seed: 1,
	}
	a := Generate(opts)
	b := Generate(opts)
	if len(a) != 10 {
		t.Fatalf("want 10 samples, got %d", len(a))
	}
	for i := range a {
		if a[i].Ts != b[i].Ts || a[i].Value != b[i].Value || a[i].Metric != b[i].Metric {
			t.Fatalf("generation not deterministic at %d: %+v vs %+v", i, a[i], b[i])
		}
		if !reflect.DeepEqual(a[i].Labels, b[i].Labels) {
			t.Fatalf("labels not deterministic at %d: %+v vs %+v", i, a[i].Labels, b[i].Labels)
		}
	}
}

func TestGenerateLabelCloning(t *testing.T) {
	labels := map[string]string{"host": "a"}
	out := Generate(Options{
		Metric: "m", Labels: labels,
		Start: 0, End: 20, PeriodSec: 10,
	})
	if len(out) != 2 {
		t.Fatalf("want 2 samples, got %d", len(out))
	}
	out[0].Labels["host"] = "MUTATED"
	if labels["host"] != "a" {
		t.Fatal("generator must clone labels into each sample")
	}
}

func TestGenerateGaps(t *testing.T) {
	out := Generate(Options{
		Metric: "m", Start: 0, End: 1000, PeriodSec: 10, GapEvery: 5,
	})
	if len(out) == 0 || len(out) >= 100 {
		t.Fatalf("gaps should reduce sample count, got %d", len(out))
	}
}

func TestGenerateJitterStaysPositiveAndBounded(t *testing.T) {
	out := Generate(Options{
		Metric: "m", Start: 1000, End: 2000, PeriodSec: 60, JitterSec: 30, Seed: 9,
	})
	if len(out) == 0 {
		t.Fatal("want samples")
	}
	for _, s := range out {
		if s.Ts < 1000 || s.Ts >= 2030 {
			t.Fatalf("jittered ts %d out of expected range", s.Ts)
		}
	}
}

func TestGenerateInvalidOptions(t *testing.T) {
	if Generate(Options{Start: 0, End: 10, PeriodSec: 0}) != nil {
		t.Fatal("zero period must yield nil")
	}
	if Generate(Options{Start: 100, End: 100, PeriodSec: 1}) != nil {
		t.Fatal("empty range must yield nil")
	}
}

func TestGenerateCarriesValues(t *testing.T) {
	out := Generate(Options{Metric: "cpu", Start: 0, End: 30, PeriodSec: 10, Offset: 42})
	for _, s := range out {
		if s.Metric != "cpu" {
			t.Fatalf("metric not carried: %+v", s)
		}
	}
}
