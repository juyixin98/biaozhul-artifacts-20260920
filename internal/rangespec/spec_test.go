package rangespec

import (
	"errors"
	"testing"
)

func TestParseHeader(t *testing.T) {
	tests := []struct {
		name    string
		header  string
		want    []Spec
		wantErr error
	}{
		{
			name:   "closed range",
			header: "bytes=0-99",
			want:   []Spec{{Kind: Closed, First: 0, Last: 99}},
		},
		{
			name:   "open ended range",
			header: "bytes=500-",
			want:   []Spec{{Kind: OpenEnded, First: 500}},
		},
		{
			name:   "suffix range",
			header: "bytes=-64",
			want:   []Spec{{Kind: Suffix, SuffixLength: 64}},
		},
		{
			name:   "multiple ranges with spaces",
			header: "bytes=0-10, 20-30 , -5",
			want: []Spec{
				{Kind: Closed, First: 0, Last: 10},
				{Kind: Closed, First: 20, Last: 30},
				{Kind: Suffix, SuffixLength: 5},
			},
		},
		{
			name:   "unit token case insensitive",
			header: "BYTES=1-2",
			want:   []Spec{{Kind: Closed, First: 1, Last: 2}},
		},
		{
			name:    "unsupported unit",
			header:  "items=0-9",
			wantErr: ErrMalformedRange,
		},
		{
			name:    "missing equals",
			header:  "bytes0-9",
			wantErr: ErrMalformedRange,
		},
		{
			name:    "first greater than last",
			header:  "bytes=9-1",
			wantErr: ErrMalformedRange,
		},
		{
			name:    "zero suffix length",
			header:  "bytes=-0",
			wantErr: ErrMalformedRange,
		},
		{
			name:    "non numeric",
			header:  "bytes=a-b",
			wantErr: ErrMalformedRange,
		},
		{
			name:    "empty range set",
			header:  "bytes=",
			wantErr: ErrMalformedRange,
		},
		{
			name:    "empty element in set",
			header:  "bytes=0-9,,10-11",
			wantErr: ErrMalformedRange,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ParseHeader(tc.header)
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("err = %v, want %v", err, tc.wantErr)
			}
			if tc.wantErr != nil {
				return
			}
			if len(got) != len(tc.want) {
				t.Fatalf("got %d specs %v, want %d %v", len(got), got, len(tc.want), tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Errorf("spec[%d] = %+v, want %+v", i, got[i], tc.want[i])
				}
			}
		})
	}
}

func TestSpecString(t *testing.T) {
	tests := []struct {
		spec Spec
		want string
	}{
		{Spec{Kind: Closed, First: 1, Last: 2}, "bytes=1-2"},
		{Spec{Kind: OpenEnded, First: 3}, "bytes=3-"},
		{Spec{Kind: Suffix, SuffixLength: 4}, "bytes=-4"},
	}
	for _, tc := range tests {
		if got := tc.spec.String(); got != tc.want {
			t.Errorf("%+v.String() = %q, want %q", tc.spec, got, tc.want)
		}
	}
}
