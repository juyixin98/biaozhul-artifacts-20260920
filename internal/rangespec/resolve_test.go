package rangespec

import (
	"errors"
	"strings"
	"testing"
)

func TestResolve(t *testing.T) {
	tests := []struct {
		name    string
		spec    Spec
		size    int64
		want    Resolved
		wantOK  bool
		wantErr bool
	}{
		{"closed inside", Spec{Kind: Closed, First: 0, Last: 9}, 100, Resolved{0, 9}, true, false},
		{"closed clamps to end", Spec{Kind: Closed, First: 90, Last: 500}, 100, Resolved{90, 99}, true, false},
		{"closed starts at last byte", Spec{Kind: Closed, First: 99, Last: 99}, 100, Resolved{99, 99}, true, false},
		{"closed starts beyond end", Spec{Kind: Closed, First: 100, Last: 200}, 100, Resolved{}, false, false},
		{"open ended", Spec{Kind: OpenEnded, First: 90}, 100, Resolved{90, 99}, true, false},
		{"open ended at last byte", Spec{Kind: OpenEnded, First: 99}, 100, Resolved{99, 99}, true, false},
		{"open ended beyond end", Spec{Kind: OpenEnded, First: 100}, 100, Resolved{}, false, false},
		{"suffix partial", Spec{Kind: Suffix, SuffixLength: 10}, 100, Resolved{90, 99}, true, false},
		{"suffix larger than size", Spec{Kind: Suffix, SuffixLength: 500}, 100, Resolved{0, 99}, true, false},
		{"suffix equals size", Spec{Kind: Suffix, SuffixLength: 100}, 100, Resolved{0, 99}, true, false},
		{"suffix on empty", Spec{Kind: Suffix, SuffixLength: 1}, 0, Resolved{}, false, false},
		{"closed on empty", Spec{Kind: Closed, First: 0, Last: 0}, 0, Resolved{}, false, false},
		{"open ended on empty", Spec{Kind: OpenEnded, First: 0}, 0, Resolved{}, false, false},
		{"negative size", Spec{Kind: Closed, First: 0, Last: 1}, -1, Resolved{}, false, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, ok, err := Resolve(tc.spec, tc.size)
			if (err != nil) != tc.wantErr {
				t.Fatalf("err = %v, wantErr = %v", err, tc.wantErr)
			}
			if ok != tc.wantOK {
				t.Fatalf("ok = %v, want %v", ok, tc.wantOK)
			}
			if ok && got != tc.want {
				t.Errorf("got %+v, want %+v", got, tc.want)
			}
			if got.Length() != tc.want.Last-tc.want.First+1 && tc.wantOK {
				t.Errorf("length = %d", got.Length())
			}
		})
	}
}

func TestResolveAll(t *testing.T) {
	t.Run("unsatisfiable set", func(t *testing.T) {
		_, err := ResolveAll([]Spec{{Kind: Closed, First: 500, Last: 600}}, 100)
		var u *UnsatisfiableError
		if !errors.As(err, &u) {
			t.Fatalf("err = %v, want *UnsatisfiableError", err)
		}
		if u.RepresentationLength != 100 {
			t.Errorf("length = %d", u.RepresentationLength)
		}
	})

	t.Run("mixed satisfiable and unsatisfiable", func(t *testing.T) {
		got, err := ResolveAll([]Spec{
			{Kind: Closed, First: 0, Last: 9},
			{Kind: Closed, First: 500, Last: 600},
		}, 100)
		if err != nil {
			t.Fatalf("unexpected err %v", err)
		}
		if len(got) != 1 || got[0] != (Resolved{0, 9}) {
			t.Errorf("got %v", got)
		}
	})

	t.Run("overlapping intervals coalesce", func(t *testing.T) {
		got, err := ResolveAll([]Spec{
			{Kind: Closed, First: 100, Last: 255},
			{Kind: Closed, First: 0, Last: 119},
		}, 256)
		if err != nil {
			t.Fatalf("unexpected err %v", err)
		}
		if len(got) != 1 || got[0] != (Resolved{0, 255}) {
			t.Errorf("got %v, want single [0,255]", got)
		}
	})

	t.Run("duplicate intervals coalesce", func(t *testing.T) {
		got, _ := ResolveAll([]Spec{
			{Kind: Closed, First: 0, Last: 9},
			{Kind: Closed, First: 0, Last: 9},
		}, 100)
		if len(got) != 1 || got[0] != (Resolved{0, 9}) {
			t.Errorf("got %v", got)
		}
	})

	t.Run("adjacent intervals stay separate", func(t *testing.T) {
		got, _ := ResolveAll([]Spec{
			{Kind: Closed, First: 0, Last: 99},
			{Kind: Closed, First: 100, Last: 199},
		}, 200)
		if len(got) != 2 {
			t.Errorf("got %v, want two adjacent parts", got)
		}
	})
}

func TestCheckCount(t *testing.T) {
	if err := CheckCount(make([]Spec, 5), 5); err != nil {
		t.Errorf("at-limit request rejected: %v", err)
	}
	if err := CheckCount(make([]Spec, 6), 5); !errors.Is(err, ErrTooManyRanges) {
		t.Errorf("over-limit err = %v, want ErrTooManyRanges", err)
	}
	if err := CheckCount(nil, 0); err == nil {
		t.Error("non-positive limit should error")
	}
}

func TestContentRange(t *testing.T) {
	if got := ContentRange(Resolved{0, 99}, 256); got != "bytes 0-99/256" {
		t.Errorf("got %q", got)
	}
	if got := UnsatisfiableContentRange(0); got != "bytes */0" {
		t.Errorf("got %q", got)
	}
}

func TestMultipartAssembler(t *testing.T) {
	rep := []byte("0123456789")
	parts := []Part{
		{Range: Resolved{0, 1}, ContentType: "application/octet-stream"},
		{Range: Resolved{8, 9}, ContentType: "application/octet-stream"},
	}
	m := NewMultipartAssembler("BOUND", rep, parts)

	body := m.Body()
	wantSubstr := []string{
		"--BOUND\r\n",
		"Content-Range: bytes 0-1/10\r\n",
		"01",
		"Content-Range: bytes 8-9/10\r\n",
		"89",
		"--BOUND--\r\n",
	}
	for _, w := range wantSubstr {
		if !strings.Contains(string(body), w) {
			t.Errorf("body missing %q\nbody:\n%q", w, body)
		}
	}
	if got := m.ContentType(); got != "multipart/byteranges; boundary=BOUND" {
		t.Errorf("content type = %q", got)
	}
}
