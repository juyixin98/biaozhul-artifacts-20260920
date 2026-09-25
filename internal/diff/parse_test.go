package diff

import (
	"strings"
	"testing"
)

func TestParseBasicModify(t *testing.T) {
	patch := `diff --git a/hello.txt b/hello.txt
index 3b18e51..e69de29 100644
--- a/hello.txt
+++ b/hello.txt
@@ -1,3 +1,3 @@
 line one
-line two
+line 2
 line three
`
	fps, err := Parse(patch)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if len(fps) != 1 {
		t.Fatalf("got %d file patches, want 1", len(fps))
	}
	fp := fps[0]
	if fp.OldPath != "hello.txt" || fp.NewPath != "hello.txt" {
		t.Fatalf("paths = %q, %q", fp.OldPath, fp.NewPath)
	}
	if fp.IsNew() || fp.IsDelete() {
		t.Fatal("modify patch misclassified")
	}
	h := fp.Hunks[0]
	if h.OldStart != 1 || h.OldCount != 3 || h.NewStart != 1 || h.NewCount != 3 {
		t.Fatalf("hunk header = -%d,%d +%d,%d", h.OldStart, h.OldCount, h.NewStart, h.NewCount)
	}
	if len(h.Lines) != 4 {
		t.Fatalf("hunk has %d lines, want 4", len(h.Lines))
	}
	want := []Line{
		{Context, "line one"},
		{Delete, "line two"},
		{Add, "line 2"},
		{Context, "line three"},
	}
	for i, w := range want {
		if h.Lines[i] != w {
			t.Errorf("line %d = %+v, want %+v", i, h.Lines[i], w)
		}
	}
}

func TestParseNewFileNoTrailingNewline(t *testing.T) {
	patch := `--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+hello
\ No newline at end of file
`
	fps, err := Parse(patch)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	fp := fps[0]
	if !fp.IsNew() {
		t.Fatal("not classified as new file")
	}
	if fp.NewPath != "new.txt" {
		t.Fatalf("NewPath = %q", fp.NewPath)
	}
	h := fp.Hunks[0]
	if h.OldStart != 0 || h.OldCount != 0 || h.NewStart != 1 || h.NewCount != 1 {
		t.Fatalf("hunk header = -%d,%d +%d,%d", h.OldStart, h.OldCount, h.NewStart, h.NewCount)
	}
	if !h.NewNoNewline {
		t.Fatal("NewNoNewline marker lost")
	}
	if h.OldNoNewline {
		t.Fatal("OldNoNewline should not be set")
	}
}

func TestParseDeletedFile(t *testing.T) {
	patch := `diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-goodbye
-world
`
	fps, err := Parse(patch)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	fp := fps[0]
	if !fp.IsDelete() {
		t.Fatal("not classified as deletion")
	}
	if fp.OldPath != "old.txt" {
		t.Fatalf("OldPath = %q", fp.OldPath)
	}
}

func TestParseNoNewlineOnBothSides(t *testing.T) {
	patch := `--- a/f
+++ b/f
@@ -1 +1 @@
-old
\ No newline at end of file
+new
\ No newline at end of file
`
	fps, err := Parse(patch)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	h := fps[0].Hunks[0]
	if !h.OldNoNewline || !h.NewNoNewline {
		t.Fatalf("markers = old:%v new:%v, want both true", h.OldNoNewline, h.NewNoNewline)
	}
}

func TestParseErrors(t *testing.T) {
	cases := map[string]string{
		"empty document":      "",
		"no file header":      "@@ -1 +1 @@\n-a\n+b\n",
		"missing +++ header":  "--- a/f\n@@ -1 +1 @@\n-a\n+b\n",
		"bad hunk header":     "--- a/f\n+++ b/f\n@@ nonsense @@\n-a\n",
		"count mismatch":      "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n-a\n+b\n",
		"truncated hunk":      "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\n-b\n+c\n",
		"unexpected line":     "--- a/f\n+++ b/f\n@@ -1 +1 @@\n?what\n",
		"both /dev/null":      "--- /dev/null\n+++ /dev/null\n@@ -0,0 +0,0 @@\n",
		"quoted path":         "--- \"a/weird name\"\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n",
		"marker without line": "--- /dev/null\n+++ b/f\n@@ -0,0 +1 @@\n\\ No newline at end of file\n+x\n",
	}
	for name, patch := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := Parse(patch); err == nil {
				t.Fatalf("Parse(%q) succeeded, want error", patch)
			}
		})
	}
}

func TestParseMultipleFilesAndHunks(t *testing.T) {
	patch := `--- a/one.txt
+++ b/one.txt
@@ -1 +1 @@
-a
+b
--- a/two.txt
+++ b/two.txt
@@ -1 +1 @@
-c
+d
@@ -3 +3 @@
-e
+f
`
	fps, err := Parse(patch)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if len(fps) != 2 {
		t.Fatalf("got %d file patches, want 2", len(fps))
	}
	if len(fps[1].Hunks) != 2 {
		t.Fatalf("second file has %d hunks, want 2", len(fps[1].Hunks))
	}
}

func TestParseStripsTimestamps(t *testing.T) {
	patch := "--- a/f.txt\t2026-09-24 10:00:00 +0000\n+++ b/f.txt\t2026-09-24 11:00:00 +0000\n@@ -1 +1 @@\n-a\n+b\n"
	fps, err := Parse(patch)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if fps[0].OldPath != "f.txt" || fps[0].NewPath != "f.txt" {
		t.Fatalf("paths = %q, %q", fps[0].OldPath, fps[0].NewPath)
	}
}

func TestParseBareEmptyLineIsEmptyContext(t *testing.T) {
	// Hand-written patches often drop the leading space on empty context
	// lines; tolerate that.
	patch := "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\n\n-b\n+c\n"
	fps, err := Parse(patch)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	h := fps[0].Hunks[0]
	if h.Lines[1].Kind != Context || h.Lines[1].Text != "" {
		t.Fatalf("line 1 = %+v, want empty context", h.Lines[1])
	}
	if !strings.Contains(patch, "\n\n") {
		t.Fatal("test premise broken")
	}
}
