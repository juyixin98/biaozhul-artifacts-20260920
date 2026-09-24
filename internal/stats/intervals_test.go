package stats

import (
	"math"
	"strconv"
	"testing"
)

func approxEq(t *testing.T, got, want, tol float64, name string) {
	t.Helper()
	if math.Abs(got-want) > tol {
		t.Fatalf("%s = %.8f, want %.8f (±%.1e)", name, got, want, tol)
	}
}

func TestNormalInvCDF(t *testing.T) {
	// Known standard-normal quantiles.
	cases := []struct{ p, want float64 }{
		{0.5, 0},
		{0.8413447460685429, 1.0},
		{0.975, 1.959963984540054},
		{0.995, 2.5758293035489004},
		{0.025, -1.959963984540054},
		{0.005, -2.5758293035489004},
	}
	for _, c := range cases {
		got := NormalInvCDF(c.p)
		approxEq(t, got, c.want, 2e-6, "NormalInvCDF("+trimFloat(c.p)+")")
	}
	if got := ZScore95(); math.Abs(got-1.95996398) > 1e-7 {
		t.Fatalf("ZScore95=%v want ~1.96", got)
	}
}

func trimFloat(f float64) string {
	return strconv.FormatFloat(f, 'g', 6, 64)
}

func TestWilsonInterval(t *testing.T) {
	// 0/100: point 0, upper 95% bound ~0.03699.
	lo, hi, err := WilsonInterval(0, 100)
	if err != nil {
		t.Fatal(err)
	}
	approxEq(t, lo, 0.000356, 5e-4, "wilson 0/100 lo")
	approxEq(t, hi, 0.03699, 5e-4, "wilson 0/100 hi")

	// 50/1000 = 5%, interval ~[0.03826, 0.06518] — point at the threshold
	// still BREACHES because the upper bound exceeds 5%. That is the intended
	// conservative behavior.
	lo, hi, err = WilsonInterval(50, 1000)
	if err != nil {
		t.Fatal(err)
	}
	approxEq(t, lo, 0.03826, 5e-4, "wilson 50/1000 lo")
	approxEq(t, hi, 0.06518, 5e-4, "wilson 50/1000 hi")

	// 0/440: upper ~0.00875.
	_, hi, err = WilsonInterval(0, 440)
	if err != nil {
		t.Fatal(err)
	}
	approxEq(t, hi, 0.00875, 5e-4, "wilson 0/440 hi")

	if _, _, err := WilsonInterval(0, 0); err == nil {
		t.Fatal("expected error for n=0")
	}
	if _, _, err := WilsonInterval(10, 5); err == nil {
		t.Fatal("expected error for errors>n")
	}
}

func TestWilsonMonotonic(t *testing.T) {
	// More errors at fixed n => strictly wider / higher upper bound.
	_, hi1, _ := WilsonInterval(0, 500)
	_, hi2, _ := WilsonInterval(20, 500)
	_, hi3, _ := WilsonInterval(100, 500)
	if !(hi1 < hi2 && hi2 < hi3) {
		t.Fatalf("wilson upper bounds not monotonic: %f %f %f", hi1, hi2, hi3)
	}
}

func TestStudentTQuantile(t *testing.T) {
	// Known t criticals for two-sided 95% (upper tail 0.025).
	cases := []struct {
		nu   float64
		want float64
	}{
		{2, 4.3026527},
		{5, 2.5705818},
		{10, 2.2281388},
		{30, 2.0422725},
	}
	for _, c := range cases {
		got := StudentTQuantile(0.975, c.nu)
		approxEq(t, got, c.want, 2e-5, "t_0.975")
		gotNeg := StudentTQuantile(0.025, c.nu)
		approxEq(t, gotNeg, -c.want, 2e-5, "t_0.025 symmetry")
	}
	// As nu grows, t converges to z.
	got := StudentTQuantile(0.975, 100000)
	approxEq(t, got, 1.959987, 2e-4, "t large df ~ z")
	if StudentTQuantile(0.5, 10) != 0 {
		t.Fatal("median of t must be 0")
	}
}

func TestMeanTInterval(t *testing.T) {
	// A symmetric constructed sample.
	samples := []float64{90, 100, 110, 95, 105, 100}
	mi, err := MeanTInterval(samples)
	if err != nil {
		t.Fatal(err)
	}
	approxEq(t, mi.Mean, 100, 1e-9, "mean")
	if !(mi.Lower < mi.Mean && mi.Mean < mi.Upper) {
		t.Fatalf("mean not inside interval: %+v", mi)
	}
	if mi.N != 6 || mi.DegFree != 5 {
		t.Fatalf("unexpected n/df: %+v", mi)
	}
	// Wider spread => wider interval.
	narrow, _ := MeanTInterval([]float64{100, 101, 99, 100})
	wide, _ := MeanTInterval([]float64{0, 200, 50, 150})
	if wide.Upper-wide.Lower <= narrow.Upper-narrow.Lower {
		t.Fatal("higher variance did not widen interval")
	}
	if _, err := MeanTInterval([]float64{100}); err == nil {
		t.Fatal("n=1 must fail")
	}
}
