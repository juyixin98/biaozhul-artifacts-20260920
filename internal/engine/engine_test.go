package engine

import "testing"

var th = Thresholds{MaxErrorRate: 0.05, MaxP95LatencyMs: 500}

func window(samples, errors int64, p95 float64, expected, received int) *MetricsWindow {
	buckets := make([]Bucket, 0, received)
	for i := 0; i < received; i++ {
		buckets = append(buckets, Bucket{
			Start: "2026-09-23T00:00:00Z", End: "2026-09-23T00:05:00Z",
			Samples: samples / int64(received), Errors: errors / int64(received), P95LatencyMs: p95,
		})
	}
	return &MetricsWindow{
		ExpectedBuckets: expected, ReceivedBuckets: received,
		Buckets: buckets, Samples: samples, Errors: errors, P95LatencyMs: p95,
	}
}

func TestStagesAndWeights(t *testing.T) {
	want := []int{5, 20, 50, 100}
	for i, w := range want {
		if StageWeights[i] != w {
			t.Fatalf("stage %d weight = %d, want %d", i, StageWeights[i], w)
		}
	}
	if next, done, err := NextStage(2); err != nil || next != 3 || done {
		t.Fatalf("NextStage(2) = %d,%v,%v", next, done, err)
	}
	if next, done, err := NextStage(3); err != nil || next != 3 || !done {
		t.Fatalf("NextStage(3) = %d,%v,%v, want 3,true", next, done, err)
	}
}

func TestEvaluate(t *testing.T) {
	cases := []struct {
		name    string
		in      Input
		verdict string
		reason  string
		can     bool
	}{
		{
			name:    "no metrics at all -> unknown, not healthy",
			in:      Input{StageIdx: 0, MinSamples: 100, WindowComplete: true, Window: nil, Threshold: th},
			verdict: VerdictUnknown, reason: ReasonMetricsMissing,
		},
		{
			name: "partial coverage -> unknown",
			in: Input{StageIdx: 0, MinSamples: 100, WindowComplete: true,
				Window: window(300, 0, 120, 4, 3), Threshold: th},
			verdict: VerdictUnknown, reason: ReasonMetricsPartial,
		},
		{
			name: "healthy but window not elapsed -> hold",
			in: Input{StageIdx: 0, MinSamples: 100, WindowComplete: false,
				Window: window(300, 0, 120, 4, 4), Threshold: th},
			verdict: VerdictHold, reason: ReasonWindowIncomplete,
		},
		{
			name: "window complete but not enough samples -> hold",
			in: Input{StageIdx: 0, MinSamples: 1000, WindowComplete: true,
				Window: window(20, 0, 120, 4, 4), Threshold: th},
			verdict: VerdictHold, reason: ReasonInsufficientSamples,
		},
		{
			name: "sustained error-rate degradation -> violation",
			in: Input{StageIdx: 0, MinSamples: 100, WindowComplete: true,
				Window: window(400, 24, 800, 4, 4), Threshold: th}, // 6% > 5%
			verdict: VerdictViolation, reason: ReasonErrorRateExceeded,
		},
		{
			name: "latency threshold exceeded -> violation",
			in: Input{StageIdx: 0, MinSamples: 100, WindowComplete: true,
				Window: window(400, 0, 650, 4, 4), Threshold: th},
			verdict: VerdictViolation, reason: ReasonLatencyExceeded,
		},
		{
			name: "transient spike diluted across full window -> promote",
			// 4 桶：一桶 8/100 错误 + 900ms，其余 3 桶健康；聚合 2% 错误率，加权 p95=315ms
			in: Input{StageIdx: 0, MinSamples: 100, WindowComplete: true,
				Window: &MetricsWindow{ExpectedBuckets: 4, ReceivedBuckets: 4, Samples: 400, Errors: 8,
					P95LatencyMs: 315, Buckets: []Bucket{
						{Samples: 100, Errors: 0, P95LatencyMs: 120},
						{Samples: 100, Errors: 8, P95LatencyMs: 900},
						{Samples: 100, Errors: 0, P95LatencyMs: 120},
						{Samples: 100, Errors: 0, P95LatencyMs: 120},
					}}, Threshold: th},
			verdict: VerdictPromote, reason: ReasonHealthy, can: true,
		},
		{
			name: "healthy full window with enough samples -> promote",
			in: Input{StageIdx: 0, MinSamples: 100, WindowComplete: true,
				Window: window(400, 0, 120, 4, 4), Threshold: th},
			verdict: VerdictPromote, reason: ReasonHealthy, can: true,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := Evaluate(tc.in)
			if d.Verdict != tc.verdict || d.Reason != tc.reason || d.CanPromote != tc.can {
				t.Fatalf("got verdict=%s reason=%s can=%v; want %s/%s/%v; decision=%+v",
					d.Verdict, d.Reason, d.CanPromote, tc.verdict, tc.reason, tc.can, d)
			}
		})
	}
}

func TestUnknownNeverReportsHealthyValues(t *testing.T) {
	d := Evaluate(Input{StageIdx: 0, WindowComplete: true, Window: nil, Threshold: th})
	if d.ErrorRate != nil || d.P95LatencyMs != nil {
		t.Fatalf("unknown verdict must carry nil metrics, got %+v", d)
	}
	d2 := Evaluate(Input{StageIdx: 0, WindowComplete: true,
		Window: window(300, 0, 120, 4, 3), Threshold: th})
	if d2.ErrorRate != nil || d2.P95LatencyMs != nil {
		t.Fatalf("partial verdict must carry nil metrics, got %+v", d2)
	}
}
