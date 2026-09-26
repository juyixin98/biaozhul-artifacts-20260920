package rangespec

import (
	"errors"
	"testing"
)

func TestParseValidForms(t *testing.T) {
	cases := []struct {
		name   string
		header string
		want   []RawRange
	}{
		{"prefix", "bytes=0-99", []RawRange{{First: 0, Last: 99}}},
		{"open", "bytes=100-", []RawRange{{First: 100, OpenEnded: true}}},
		{"suffix", "bytes=-200", []RawRange{{Suffix: true, SuffixLen: 200}}},
		{"case insensitive unit", "BYTES=0-1", []RawRange{{First: 0, Last: 1}}},
		{"whitespace tolerated around members", "bytes= 0-1 , 2-3 ", []RawRange{
			{First: 0, Last: 1}, {First: 2, Last: 3},
		}},
		{"zero suffix", "bytes=-0", []RawRange{{Suffix: true, SuffixLen: 0}}},
		{"first equals last", "bytes=5-5", []RawRange{{First: 5, Last: 5}}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Parse(tc.header)
			if err != nil {
				t.Fatalf("Parse(%q): %v", tc.header, err)
			}
			if len(got) != len(tc.want) {
				t.Fatalf("got %d ranges, want %d", len(got), len(tc.want))
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Errorf("range %d = %+v, want %+v", i, got[i], tc.want[i])
				}
			}
		})
	}
}

func TestParseInvalid(t *testing.T) {
	bad := []string{
		"",
		"bytes=",
		"items=0-9",
		"bytes=abc",
		"bytes=0-1,",
		"bytes=0 -1",
		"bytes=9-5",
		"bytes=0--1",
		"bytes=-",
		"bytes=1.5-2",
		"bytes=999999999999999999999999-1",
	}
	for _, h := range bad {
		_, err := Parse(h)
		if !errors.Is(err, ErrInvalidRange) {
			t.Errorf("Parse(%q) err = %v, want ErrInvalidRange", h, err)
		}
	}
}

func TestRawRangeString(t *testing.T) {
	if got := (RawRange{First: 1, Last: 2}).String(); got != "1-2" {
		t.Errorf("closed: %q", got)
	}
	if got := (RawRange{First: 3, OpenEnded: true}).String(); got != "3-" {
		t.Errorf("open: %q", got)
	}
	if got := (RawRange{Suffix: true, SuffixLen: 4}).String(); got != "-4" {
		t.Errorf("suffix: %q", got)
	}
}
