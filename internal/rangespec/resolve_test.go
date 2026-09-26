package rangespec

import (
	"errors"
	"testing"
)

func TestResolveSingle(t *testing.T) {
	raw := func(hs ...string) []RawRange {
		var out []RawRange
		for _, h := range hs {
			rr, err := Parse("bytes=" + h)
			if err != nil {
				t.Fatalf("setup Parse: %v", err)
			}
			out = append(out, rr...)
		}
		return out
	}
	cases := []struct {
		name      string
		spec      string
		size      int64
		wantStart int64
		wantEnd   int64
	}{
		{"prefix", "0-99", 1024, 0, 99},
		{"open clamps end", "1000-", 1024, 1000, 1023},
		{"suffix", "-200", 1024, 824, 1023},
		{"suffix bigger than size", "-5000", 1024, 0, 1023},
		{"last beyond end clamped", "900-5000", 1024, 900, 1023},
		{"single byte at end", "1023-1023", 1024, 1023, 1023},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Resolve(raw(tc.spec), tc.size)
			if err != nil {
				t.Fatalf("Resolve: %v", err)
			}
			if len(got) != 1 || got[0].Start != tc.wantStart || got[0].End != tc.wantEnd {
				t.Fatalf("got %+v, want [%d-%d]", got, tc.wantStart, tc.wantEnd)
			}
			if got[0].Length() != tc.wantEnd-tc.wantStart+1 {
				t.Errorf("length wrong")
			}
		})
	}
}

func TestResolveUnsatisfiable(t *testing.T) {
	raw := func(s string) []RawRange {
		rr, err := Parse("bytes=" + s)
		if err != nil {
			t.Fatalf("setup: %v", err)
		}
		return rr
	}
	cases := []struct {
		spec string
		size int64
	}{
		{"1024-", 1024},
		{"2000-3000", 1024},
		{"0-0", 0},
		{"-100", 0},
		{"-0", 10}, // suffix of zero yields no bytes
	}
	for _, tc := range cases {
		_, err := Resolve(raw(tc.spec), tc.size)
		if !errors.Is(err, ErrUnsatisfiable) {
			t.Errorf("Resolve(%q, %d) err = %v, want ErrUnsatisfiable", tc.spec, tc.size, err)
		}
	}
}

func TestResolveMixedSetDropsOnlyUnsatisfiableMembers(t *testing.T) {
	rr, err := Parse("bytes=0-9,5000-6000,-5")
	if err != nil {
		t.Fatal(err)
	}
	got, err := Resolve(rr, 100)
	if err != nil {
		t.Fatalf("set with one good member must be satisfiable: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("want 2 surviving ranges, got %d: %+v", len(got), got)
	}
}

func TestResolvePreservesOverlapsAndDuplicates(t *testing.T) {
	rr, _ := Parse("bytes=0-10,5-15,5-15")
	got, err := Resolve(rr, 100)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 3 {
		t.Fatalf("want 3 preserved members, got %d", len(got))
	}
}

func TestContentRangeRendering(t *testing.T) {
	r := Resolved{Start: 10, End: 19}
	if got := r.ContentRange(100); got != "bytes 10-19/100" {
		t.Errorf("ContentRange = %q", got)
	}
	if got := UnsatisfiableContentRange(0); got != "bytes */0" {
		t.Errorf("unsatisfiable = %q", got)
	}
}
