package detection

import (
	"math"
	"testing"
)

func TestMeanStddev(t *testing.T) {
	mean, sd := MeanStddev([]float64{2, 4, 4, 4, 5, 5, 7, 9})
	if math.Abs(mean-5) > 1e-9 {
		t.Fatalf("mean = %v, want 5", mean)
	}
	// Population stddev of this set is 2.
	if math.Abs(sd-2) > 1e-9 {
		t.Fatalf("stddev = %v, want 2", sd)
	}
	if m, _ := MeanStddev(nil); m != 0 {
		t.Fatalf("empty mean = %v, want 0", m)
	}
}

func TestZScore(t *testing.T) {
	if z := ZScore(5, 2, 10); math.Abs(z-2.5) > 1e-9 {
		t.Fatalf("z = %v, want 2.5", z)
	}
	if z := ZScore(5, 0, 6); !math.IsInf(z, 1) {
		t.Fatalf("zero-stddev above mean should be +Inf, got %v", z)
	}
	if z := ZScore(5, 0, 5); z != 0 {
		t.Fatalf("zero-stddev equal should be 0, got %v", z)
	}
	if z := ZScore(5, 0, 4); !math.IsInf(z, -1) {
		t.Fatalf("zero-stddev below mean should be -Inf, got %v", z)
	}
}
