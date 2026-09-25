package scanner

import (
	"errors"
	"testing"
)

func TestScan(t *testing.T) {
	src := `#include <stdio.h>
#include "local.h"
  #  include   <spaced.h>
#include "path/nested.h"
`
	got, err := Scan(src)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := []Include{
		{Path: "stdio.h", Angle: true, Line: 1},
		{Path: "local.h", Angle: false, Line: 2},
		{Path: "spaced.h", Angle: true, Line: 3},
		{Path: "path/nested.h", Angle: false, Line: 4},
	}
	if len(got) != len(want) {
		t.Fatalf("got %d includes, want %d: %+v", len(got), len(want), got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("include %d = %+v, want %+v", i, got[i], want[i])
		}
	}
}

func TestScanIgnoresPseudoIncludesInComments(t *testing.T) {
	src := `#include "real.h"
// #include "line-comment.h"
/* #include "block-comment.h" */
/* multi
   line #include <block-angle.h>
   block */
printf("#include <inside-string.h>");
`
	got, err := Scan(src)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(got) != 1 || got[0].Path != "real.h" {
		t.Fatalf("only real.h should be detected, got %+v", got)
	}
}

func TestScanUnterminatedBlockComment(t *testing.T) {
	src := "#include \"real.h\"\n/* unterminated #include \"fake.h\"\n"
	got, err := Scan(src)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(got) != 1 || got[0].Path != "real.h" {
		t.Fatalf("unterminated block comment must be ignored, got %+v", got)
	}
}

func TestScanMacroIncludeIsError(t *testing.T) {
	for _, src := range []string{
		"#include GENERATED_H\n",
		"#include CAT(foo, .h)\n",
	} {
		_, err := Scan(src)
		if !errors.Is(err, ErrMacroInclude) {
			t.Errorf("src=%q: error = %v, want ErrMacroInclude", src, err)
		}
	}
}

func TestScanMacroIncludeReportsLine(t *testing.T) {
	src := "#include \"a.h\"\n#include VAR\n"
	_, err := Scan(src)
	if err == nil {
		t.Fatal("expected error")
	}
	if msg := err.Error(); !contains(msg, "line 2") {
		t.Errorf("error %q does not report line 2", msg)
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (indexOf(s, sub) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
