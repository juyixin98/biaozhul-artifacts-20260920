// Package seed builds deterministic synthetic counter data for demos and
// tests. It is the only "monitoring data" in this project: no real platform.
package seed

import (
	"counterreset/counter"
)

// Batch is one series worth of synthetic samples.
type Batch struct {
	Metric  string
	Labels  map[string]string
	Samples []counter.Sample
}

// MainSeries is the hand-verifiable acceptance sequence (see README §"手算").
//
//	t=0..180  monotonic 10 -> 50 in steps of 10
//	t=240     missing on purpose (60->120 gap), value at 240 would be 60
//	t=300     70
//	t=360..480 80 -> 100
//	t=540     5   <- counter reset (100 -> 5)
//	t=600     15
func MainSeries() Batch {
	return Batch{
		Metric: "requests_total",
		Labels: map[string]string{"service": "demo", "host": "synthetic-1"},
		Samples: []counter.Sample{
			{T: 0, Value: 10},
			{T: 60, Value: 20},
			{T: 120, Value: 30},
			{T: 180, Value: 40},
			// t=240 deliberately absent: missing sample / gap.
			{T: 300, Value: 70},
			{T: 360, Value: 80},
			{T: 420, Value: 90},
			{T: 480, Value: 100},
			{T: 540, Value: 5}, // reset
			{T: 600, Value: 15},
		},
	}
}

// OutOfOrderSeries is ingested shuffled, to demonstrate that arrival order
// does not affect results.
func OutOfOrderSeries() Batch {
	return Batch{
		Metric: "events_total",
		Labels: map[string]string{"service": "demo", "host": "synthetic-2"},
		Samples: []counter.Sample{
			{T: 120, Value: 3},
			{T: 0, Value: 0},
			{T: 180, Value: 6},
			{T: 60, Value: 2},
			{T: 240, Value: 1}, // reset, arrives late/early arbitrarily
		},
	}
}

// All returns every seed batch.
func All() []Batch {
	return []Batch{MainSeries(), OutOfOrderSeries()}
}
