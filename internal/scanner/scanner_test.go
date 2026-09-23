package scanner

import (
	"strings"
	"testing"
)

func TestExtract_BasicQuotedAndAngled(t *testing.T) {
	src := `#include <stdio.h>
#include "foo/bar.h"
  #  include   <spaced.h>
int main(void) { return 0; }
`
	incs, diags := Extract("x.c", []byte(src))
	if len(diags) != 0 {
		t.Fatalf("unexpected diagnostics: %+v", diags)
	}
	want := []Include{
		{Path: "stdio.h", Kind: Angled, Line: 1},
		{Path: "foo/bar.h", Kind: Quoted, Line: 2},
		{Path: "spaced.h", Kind: Angled, Line: 3},
	}
	if len(incs) != len(want) {
		t.Fatalf("got %d includes, want %d: %+v", len(incs), len(want), incs)
	}
	for i := range want {
		if incs[i] != want[i] {
			t.Errorf("include[%d] = %+v, want %+v", i, incs[i], want[i])
		}
	}
}

func TestExtract_PseudoIncludesInCommentsAndStrings(t *testing.T) {
	src := `// #include "line-comment.h"
/* #include "block-comment.h" */
/* 跨行块注释
   #include "inside-block.h"
*/
const char *s = "#include \"in-string.h\"";
const char c = '"';
char q = '\'';
/* 普通内容 */
#include "real.h"
`
	incs, diags := Extract("x.c", []byte(src))
	if len(diags) != 0 {
		t.Fatalf("unexpected diagnostics: %+v", diags)
	}
	if len(incs) != 1 || incs[0].Path != "real.h" {
		t.Fatalf("expected only real.h, got %+v", incs)
	}
	if incs[0].Line != 10 {
		t.Errorf("real.h line = %d, want 10", incs[0].Line)
	}
}

func TestExtract_BackslashContinuation(t *testing.T) {
	// 续行后形成真正的 include（合法）
	srcValid := `#inc\
lude "joined.h"
`
	incs, diags := Extract("x.c", []byte(srcValid))
	if len(diags) != 0 {
		t.Fatalf("unexpected diagnostics: %+v", diags)
	}
	if len(incs) != 1 || incs[0].Path != "joined.h" {
		t.Fatalf("expected joined.h, got %+v", incs)
	}
	if incs[0].Line != 1 { // 起始物理行
		t.Errorf("line = %d, want 1", incs[0].Line)
	}

	// 行注释中的反斜杠续行：下一行被注释吞掉，不产生 include
	srcComment := `// #include "fake.h" \
#include "also-fake.h"
#include "real2.h"
`
	incs, diags = Extract("x.c", []byte(srcComment))
	if len(diags) != 0 {
		t.Fatalf("unexpected diagnostics: %+v", diags)
	}
	if len(incs) != 1 || incs[0].Path != "real2.h" {
		t.Fatalf("expected only real2.h, got %+v", incs)
	}
}

func TestExtract_MacroGeneratedIncludeIsError(t *testing.T) {
	cases := map[string]string{
		"macro":         "#include FOO\n",
		"macro_defined": "#define X \"y.h\"\n#include X\n",
		"paren_macro":   "#include(FOO)\n",
	}
	for name, src := range cases {
		t.Run(name, func(t *testing.T) {
			_, diags := Extract("x.c", []byte(src))
			found := false
			for _, d := range diags {
				if d.Severity == SeverityError && strings.Contains(d.Message, "not supported") {
					found = true
				}
			}
			if !found {
				t.Fatalf("expected unsupported-macro error for %q, got %+v", src, diags)
			}
		})
	}

	// include 后无路径：属于格式错误，同样以 error 级别失败。
	if _, diags := Extract("x.c", []byte("#include \n")); len(diags) != 1 ||
		diags[0].Severity != SeverityError {
		t.Fatalf("expected error for empty #include, got %+v", diags)
	}
}

func TestExtract_MalformedIncludes(t *testing.T) {
	cases := map[string]string{
		"unclosed_quote":  "#include \"foo.h\n",
		"unclosed_angle":  "#include <foo.h\n",
		"empty_quote":     "#include \"\"\n",
		"empty_angle":     "#include <>\n",
		"trailing_tokens": "#include \"a.h\" extra\n",
	}
	for name, src := range cases {
		t.Run(name, func(t *testing.T) {
			incs, diags := Extract("x.c", []byte(src))
			if len(incs) != 0 {
				t.Fatalf("expected no includes, got %+v", incs)
			}
			if len(diags) != 1 || diags[0].Severity != SeverityError {
				t.Fatalf("expected one error, got %+v", diags)
			}
		})
	}
}

func TestExtract_CRLFAndBOM(t *testing.T) {
	src := []byte("\xEF\xBB\xBF#include \"a.h\"\r\n#include <b.h>\r\n")
	incs, diags := Extract("x.c", src)
	if len(diags) != 0 {
		t.Fatalf("unexpected diagnostics: %+v", diags)
	}
	if len(incs) != 2 || incs[0].Path != "a.h" || incs[1].Path != "b.h" {
		t.Fatalf("unexpected includes: %+v", incs)
	}
	if incs[0].Line != 1 || incs[1].Line != 2 {
		t.Fatalf("line numbers wrong: %+v", incs)
	}
}

func TestExtract_NonIncludeDirectivesIgnored(t *testing.T) {
	src := `#define FOO 1
#ifndef X
#pragma once
#  ifdef Y
#  endif
#endif
typedef int my_int;
`
	incs, diags := Extract("x.c", []byte(src))
	if len(diags) != 0 {
		t.Fatalf("unexpected diagnostics: %+v", diags)
	}
	if len(incs) != 0 {
		t.Fatalf("expected no includes, got %+v", incs)
	}
}

func TestExtract_LineNumbersWithBlockComment(t *testing.T) {
	src := "/* one\n two\n three */\n#include \"a.h\"\n"
	incs, diags := Extract("x.c", []byte(src))
	if len(diags) != 0 {
		t.Fatalf("unexpected diagnostics: %+v", diags)
	}
	if len(incs) != 1 || incs[0].Line != 4 {
		t.Fatalf("want line 4, got %+v", incs)
	}
}

func TestExtract_EscapedQuotesInString(t *testing.T) {
	src := `const char *s = "he said \"#include \"fake.h\"";
#include "real.h"
`
	incs, diags := Extract("x.c", []byte(src))
	if len(diags) != 0 {
		t.Fatalf("unexpected diagnostics: %+v", diags)
	}
	if len(incs) != 1 || incs[0].Path != "real.h" {
		t.Fatalf("expected only real.h, got %+v", incs)
	}
}

func TestExtract_IncludeLikeWordNotDirective(t *testing.T) {
	src := `int x = 0; /* "#includer" */
#includer "nope.h"
#include "yes.h"
`
	incs, diags := Extract("x.c", []byte(src))
	if len(diags) != 0 {
		t.Fatalf("unexpected diagnostics: %+v", diags)
	}
	if len(incs) != 1 || incs[0].Path != "yes.h" {
		t.Fatalf("expected only yes.h, got %+v", incs)
	}
}
