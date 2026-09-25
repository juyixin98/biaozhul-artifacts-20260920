package model

import "testing"

func TestSeriesKey(t *testing.T) {
	cases := []struct {
		name   string
		metric string
		labels map[string]string
	}{
		{"no labels", "m", nil},
		{"empty labels", "m", map[string]string{}},
		{"one label", "m", map[string]string{"host": "a"}},
		{"multi labels", "m", map[string]string{"host": "a", "dc": "x"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			k := SeriesKey(tc.metric, tc.labels)
			// Determinism across fresh maps with different insertion order.
			reordered := map[string]string{}
			for k, v := range tc.labels {
				reordered[k] = v
			}
			if SeriesKey(tc.metric, reordered) != k {
				t.Fatal("key must be independent of map iteration order")
			}
		})
	}

	a := SeriesKey("m", map[string]string{"host": "a"})
	b := SeriesKey("m", map[string]string{"host": "b"})
	if a == b {
		t.Fatal("different label values must produce different keys")
	}
	// Guard against k=v concatenation collisions.
	k1 := SeriesKey("m", map[string]string{"a": "1", "b": "2"})
	k2 := SeriesKey("m", map[string]string{"a,b": "1,2"})
	if k1 == k2 {
		t.Fatalf("delimiter encoding collision: %s", k1)
	}
}
