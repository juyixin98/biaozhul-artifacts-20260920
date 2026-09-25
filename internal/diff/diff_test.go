package diff

import (
	"testing"
)

func TestParseSimple(t *testing.T) {
	text := "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
	fps, err := Parse([]byte(text))
	if err != nil {
		t.Fatal(err)
	}
	if len(fps) != 1 {
		t.Fatalf("got %d sections", len(fps))
	}
	fp := fps[0]
	if fp.OldPath != "f" || fp.NewPath != "f" {
		t.Fatalf("paths: %q %q", fp.OldPath, fp.NewPath)
	}
	if len(fp.Hunks) != 1 {
		t.Fatalf("hunks: %d", len(fp.Hunks))
	}
	h := fp.Hunks[0]
	if h.OldStart != 1 || h.OldLines != 2 || h.NewStart != 1 || h.NewLines != 2 {
		t.Fatalf("header: %+v", h)
	}
	if len(h.Body) != 3 {
		t.Fatalf("body lines: %d", len(h.Body))
	}
}

func TestParseGitHeader(t *testing.T) {
	text := "diff --git a/f b/f\nindex 1111111..2222222 100644\n--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\n+A\n"
	fps, err := Parse([]byte(text))
	if err != nil {
		t.Fatal(err)
	}
	if len(fps) != 1 || fps[0].Target() != "f" {
		t.Fatalf("got %+v", fps)
	}
}

func TestParseNewAndDeletedFile(t *testing.T) {
	text := "diff --git a/n b/n\nnew file mode 100644\n--- /dev/null\n+++ b/n\n@@ -0,0 +1,1 @@\n+x\n"
	fps, err := Parse([]byte(text))
	if err != nil {
		t.Fatal(err)
	}
	if !fps[0].IsCreate() {
		t.Fatal("expected create")
	}
}

func TestParseDeletedFile(t *testing.T) {
	text := "diff --git a/g b/g\ndeleted file mode 100644\n--- a/g\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n"
	fps, err := Parse([]byte(text))
	if err != nil {
		t.Fatal(err)
	}
	if !fps[0].IsDelete() || fps[0].Target() != "g" {
		t.Fatalf("got %+v", fps[0])
	}
}

func TestParseMultipleFiles(t *testing.T) {
	text := "--- a/f1\n+++ b/f1\n@@ -1,1 +1,1 @@\n-a\n+A\n--- a/f2\n+++ b/f2\n@@ -1,1 +1,1 @@\n-b\n+B\n"
	fps, err := Parse([]byte(text))
	if err != nil {
		t.Fatal(err)
	}
	if len(fps) != 2 || fps[0].Target() != "f1" || fps[1].Target() != "f2" {
		t.Fatalf("got %d: %+v", len(fps), fps)
	}
}

func TestParseNoNewlineMarker(t *testing.T) {
	text := "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\n\\ No newline at end of file\n+A\n\\ No newline at end of file\n"
	fps, err := Parse([]byte(text))
	if err != nil {
		t.Fatal(err)
	}
	h := fps[0].Hunks[0]
	var markers int
	for _, bl := range h.Body {
		if bl.Kind == '\\' {
			markers++
		}
	}
	if markers != 2 {
		t.Fatalf("markers = %d", markers)
	}
}

func TestParseHunkCountMismatch(t *testing.T) {
	text := "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n-a\n+A\n"
	_, err := Parse([]byte(text))
	if err == nil {
		t.Fatal("expected count mismatch")
	}
	de := err.(*Error)
	if de.Code != CodeCountMismatch {
		t.Fatalf("got %s", de.Code)
	}
}

func TestParseBadHunkHeader(t *testing.T) {
	text := "--- a/f\n+++ b/f\n@@ not a header @@\n-a\n"
	_, err := Parse([]byte(text))
	if err == nil {
		t.Fatal("expected bad header")
	}
	if de := err.(*Error); de.Code != CodeBadHunkHeader {
		t.Fatalf("got %s", de.Code)
	}
}

func TestParseBinaryRejected(t *testing.T) {
	text := "diff --git a/f b/f\nGIT binary patch\nliteral 10\n"
	_, err := Parse([]byte(text))
	if err == nil {
		t.Fatal("expected binary rejection")
	}
	if de := err.(*Error); de.Code != CodeBinaryUnsupported {
		t.Fatalf("got %s", de.Code)
	}
}

func TestParseRenameRejected(t *testing.T) {
	text := "--- a/old\n+++ b/new\n@@ -1,1 +1,1 @@\n-x\n+y\n"
	_, err := Parse([]byte(text))
	if err == nil {
		t.Fatal("expected rename rejection")
	}
	if de := err.(*Error); de.Code != CodeUnsupportedRename {
		t.Fatalf("got %s", de.Code)
	}
}

func TestParseEmptyPatch(t *testing.T) {
	if _, err := Parse([]byte("")); err == nil {
		t.Fatal("expected empty patch error")
	}
	if _, err := Parse([]byte("just prose\n")); err == nil {
		t.Fatal("expected unexpected-line error")
	}
}

func TestParseTimestampSuffixStripped(t *testing.T) {
	text := "--- a/f\t2026-01-02 03:04:05.000000000 +0000\n+++ b/f\t2026-01-02 03:04:05.000000000 +0000\n@@ -1,1 +1,1 @@\n-a\n+A\n"
	fps, err := Parse([]byte(text))
	if err != nil {
		t.Fatal(err)
	}
	if fps[0].Target() != "f" {
		t.Fatalf("target = %q", fps[0].Target())
	}
}

func TestParseHunkOutsideSection(t *testing.T) {
	text := "@@ -1,1 +1,1 @@\n-a\n+A\n"
	if _, err := Parse([]byte(text)); err == nil {
		t.Fatal("expected hunk-outside-section error")
	}
}

func TestParseMarkerNotFollowingContent(t *testing.T) {
	text := "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n\\ No newline at end of file\n"
	if _, err := Parse([]byte(text)); err == nil {
		t.Fatal("expected marker error")
	}
}
