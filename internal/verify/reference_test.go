package verify

import (
	"testing"

	"metricrollup/internal/model"
	"metricrollup/internal/rollup"
)

func TestReferenceAggregate(t *testing.T) {
	samples := []model.Sample{
		{Ts: 0, Value: 1},
		{Ts: 59, Value: 3},
		{Ts: 60, Value: 5},   // next minute
		{Ts: 3599, Value: 7}, // next hour, last second
		{Ts: 4000, Value: 9}, // out of range: must be ignored
	}
	got := ReferenceAggregate(samples, 0, 3600, rollup.Minute)
	// 02:00 .. 02:59 -> 60 minute buckets, minutes 1..58 empty.
	if len(got) != 60 {
		t.Fatalf("want 60 buckets incl. empties, got %d", len(got))
	}
	if got[0].Agg.Count != 2 || got[0].Agg.Sum != 4 {
		t.Fatalf("minute 0 wrong: %+v", got[0].Agg)
	}
	if got[1].Agg.Count != 1 || got[1].Agg.Sum != 5 {
		t.Fatalf("minute 1 wrong: %+v", got[1].Agg)
	}
	if got[59].Agg.Count != 1 || got[59].Agg.Sum != 7 {
		t.Fatalf("minute 59 wrong: %+v", got[59].Agg)
	}
	empty := 0
	for _, b := range got {
		if b.Agg.Count == 0 {
			empty++
		}
	}
	if empty != 57 {
		t.Fatalf("want 57 empty minutes, got %d", empty)
	}
}

func TestCompareDetectsMismatch(t *testing.T) {
	want := []ReferenceBucket{
		{Start: 0, Agg: rollup.Agg{Count: 2, Sum: 4, Min: 1, Max: 3}},
	}
	cases := []struct {
		name string
		got  []GotBucket
		want string // expected field in mismatch
	}{
		{"matching", []GotBucket{{Start: 0, Count: 2, Sum: 4, Min: 1, Max: 3}}, ""},
		{"wrong count", []GotBucket{{Start: 0, Count: 3, Sum: 4, Min: 1, Max: 3}}, "count"},
		{"wrong sum", []GotBucket{{Start: 0, Count: 2, Sum: 5, Min: 1, Max: 3}}, "sum"},
		{"wrong min", []GotBucket{{Start: 0, Count: 2, Sum: 4, Min: 2, Max: 3}}, "min"},
		{"wrong max", []GotBucket{{Start: 0, Count: 2, Sum: 4, Min: 1, Max: 9}}, "max"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			mm := Compare(tc.got, want)
			if tc.want == "" {
				if len(mm) != 0 {
					t.Fatalf("unexpected mismatch: %v", mm)
				}
				return
			}
			if len(mm) != 1 || mm[0].Field != tc.want {
				t.Fatalf("want single %s mismatch, got %v", tc.want, mm)
			}
		})
	}
}

func TestCompareEmptyBucket(t *testing.T) {
	// Empty reference bucket: comparator must not demand numeric fields.
	want := []ReferenceBucket{{Start: 0, Agg: rollup.Agg{}}}
	got := []GotBucket{{Start: 0}}
	if mm := Compare(got, want); len(mm) != 0 {
		t.Fatalf("empty buckets should match: %v", mm)
	}
}

func TestCompareBucketCountMismatch(t *testing.T) {
	if mm := Compare(nil, []ReferenceBucket{{Start: 0}}); len(mm) != 1 || mm[0].Field != "bucket_count" {
		t.Fatalf("want bucket_count mismatch, got %v", mm)
	}
}
