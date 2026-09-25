package apply

import (
	"testing"

	"patchd/internal/diff"
)

func mustParse(t *testing.T, text string) []*diff.FilePatch {
	t.Helper()
	fps, err := diff.Parse([]byte(text))
	if err != nil {
		t.Fatalf("unexpected parse error: %v", err)
	}
	return fps
}

func verifyOne(t *testing.T, patchText string, original []byte) ([]byte, Stats) {
	t.Helper()
	fps := mustParse(t, patchText)
	if len(fps) != 1 {
		t.Fatalf("expected 1 file patch, got %d", len(fps))
	}
	out, st, err := Verify(fps[0], original)
	if err != nil {
		t.Fatalf("unexpected verify error: %v", err)
	}
	return out, st
}

func TestBasicModify(t *testing.T) {
	patch := "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
	out, st := verifyOne(t, patch, []byte("a\nb\nc\n"))
	if string(out) != "a\nB\nc\n" {
		t.Fatalf("got %q", out)
	}
	if st.Added != 1 || st.Removed != 1 || st.Context != 2 {
		t.Fatalf("stats: %+v", st)
	}
}

func TestMultiHunk(t *testing.T) {
	patch := "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a1\n-a2\n+A2\n@@ -4,2 +4,2 @@\n a4\n-a5\n+A5\n"
	orig := []byte("a1\na2\na3\na4\na5\n")
	out, _ := verifyOne(t, patch, orig)
	if string(out) != "a1\nA2\na3\na4\nA5\n" {
		t.Fatalf("got %q", out)
	}
}

func TestNoTrailingNewlineOld(t *testing.T) {
	// File without a trailing newline, changed.
	patch := "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+B\n\\ No newline at end of file\n"
	out, _ := verifyOne(t, patch, []byte("a\nb"))
	if string(out) != "a\nB" {
		t.Fatalf("got %q", out)
	}
}

func TestNoTrailingNewlineContextHunk(t *testing.T) {
	// Replacing the final unterminated line.
	patch := "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+B\n\\ No newline at end of file\n"
	out, _ := verifyOne(t, patch, []byte("a\nb"))
	if string(out) != "a\nB" {
		t.Fatalf("got %q", out)
	}
}

func TestAddTrailingNewline(t *testing.T) {
	// File missing newline; patch adds one (old marker only).
	patch := "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\n\\ No newline at end of file\n+a\n"
	out, _ := verifyOne(t, patch, []byte("a"))
	if string(out) != "a\n" {
		t.Fatalf("got %q", out)
	}
}

func TestRemoveTrailingNewline(t *testing.T) {
	// File ends with newline; patch removes it (new marker only).
	patch := "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\n+a\n\\ No newline at end of file\n"
	out, _ := verifyOne(t, patch, []byte("a\n"))
	if string(out) != "a" {
		t.Fatalf("got %q", out)
	}
}

func TestCreateFile(t *testing.T) {
	patch := "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n"
	out, st := verifyOne(t, patch, nil)
	if string(out) != "hello\nworld\n" {
		t.Fatalf("got %q", out)
	}
	if st.Existed || !st.WillExist {
		t.Fatalf("stats: %+v", st)
	}
}

func TestCreateFileNoTrailingNewline(t *testing.T) {
	patch := "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,1 @@\n+hello\n\\ No newline at end of file\n"
	out, _ := verifyOne(t, patch, nil)
	if string(out) != "hello" {
		t.Fatalf("get %q", out)
	}
}

func TestCreateEmptyFile(t *testing.T) {
	patch := "--- /dev/null\n+++ b/empty\n"
	out, st := verifyOne(t, patch, nil)
	if len(out) != 0 {
		t.Fatalf("got %q", out)
	}
	if !st.WillExist {
		t.Fatalf("expected file to exist after")
	}
}

func TestDeleteFile(t *testing.T) {
	patch := "--- a/gone\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n"
	out, st, err := Verify(mustParse(t, patch)[0], []byte("x\ny\n"))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out != nil {
		t.Fatalf("expected nil result, got %q", out)
	}
	if st.WillExist {
		t.Fatalf("expected file to be removed")
	}
}

func TestDeleteEmptyFile(t *testing.T) {
	patch := "--- a/empty\n+++ /dev/null\n"
	out, _, err := Verify(mustParse(t, patch)[0], []byte{})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out != nil {
		t.Fatalf("expected nil, got %q", out)
	}
}

func TestOverlappingHunksRejected(t *testing.T) {
	// Hunk 1 covers lines 1-3, hunk 2 starts at line 3.
	patch := "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n@@ -3,1 +3,1 @@\n-c\n+C\n"
	_, _, err := Verify(mustParse(t, patch)[0], []byte("a\nb\nc\nd\n"))
	if err == nil {
		t.Fatalf("expected overlap error")
	}
	ae := err.(*Error)
	if ae.Code != CodeOverlappingHunks {
		t.Fatalf("got code %s: %v", ae.Code, err)
	}
}

func TestContextMismatchRejected(t *testing.T) {
	patch := "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-Z\n+Z2\n"
	_, _, err := Verify(mustParse(t, patch)[0], []byte("a\nb\n"))
	if err == nil {
		t.Fatalf("expected context mismatch")
	}
	if ae := err.(*Error); ae.Code != CodeContextMismatch {
		t.Fatalf("got %s: %v", ae.Code, err)
	}
}

func TestLineNumberTooFar(t *testing.T) {
	patch := "--- a/f\n+++ b/f\n@@ -5,1 +5,1 @@\n-x\n+X\n"
	_, _, err := Verify(mustParse(t, patch)[0], []byte("a\nb\n"))
	if err == nil {
		t.Fatalf("expected hunk past EOF error")
	}
	if ae := err.(*Error); ae.Code != CodeHunkTooLong {
		t.Fatalf("got %s: %v", ae.Code, err)
	}
}

func TestWrongNewStartRejected(t *testing.T) {
	patch := "--- a/f\n+++ b/f\n@@ -1,2 +5,2 @@\n a\n-b\n+B\n"
	_, _, err := Verify(mustParse(t, patch)[0], []byte("a\nb\nc\n"))
	if err == nil {
		t.Fatalf("expected new-start error")
	}
	if ae := err.(*Error); ae.Code != CodeNewStartMismatch {
		t.Fatalf("got %s", ae.Code)
	}
}

func TestMissingMarkerRejected(t *testing.T) {
	// File has no trailing newline, but the diff claims it does.
	patch := "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\n+a\n"
	_, _, err := Verify(mustParse(t, patch)[0], []byte("a"))
	if err == nil {
		t.Fatalf("expected missing marker error")
	}
	if ae := err.(*Error); ae.Code != CodeMarkerInvalid {
		t.Fatalf("got %s: %v", ae.Code, err)
	}
}

func TestSpuriousMarkerRejected(t *testing.T) {
	// File ends with newline, diff claims it does not.
	patch := "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\n\\ No newline at end of file\n+a\n\\ No newline at end of file\n"
	_, _, err := Verify(mustParse(t, patch)[0], []byte("a\n"))
	if err == nil {
		t.Fatalf("expected marker error")
	}
	if ae := err.(*Error); ae.Code != CodeMarkerInvalid {
		t.Fatalf("got %s", ae.Code)
	}
}

func TestCreateOnExistingRejected(t *testing.T) {
	patch := "--- /dev/null\n+++ b/f\n@@ -0,0 +1,1 @@\n+x\n"
	_, _, err := Verify(mustParse(t, patch)[0], []byte("x\n"))
	if err == nil {
		t.Fatalf("expected already-exists error")
	}
	if ae := err.(*Error); ae.Code != CodeAlreadyExists {
		t.Fatalf("got %s", ae.Code)
	}
}

func TestModifyMissingRejected(t *testing.T) {
	patch := "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\n+A\n"
	_, _, err := Verify(mustParse(t, patch)[0], nil)
	if err == nil {
		t.Fatalf("expected not-found error")
	}
	if ae := err.(*Error); ae.Code != CodePathNotFound {
		t.Fatalf("got %s", ae.Code)
	}
}

func TestPureInsertion(t *testing.T) {
	// Insert a line between two context lines.
	proper := "--- a/f\n+++ b/f\n@@ -1,2 +1,3 @@\n a\n+inserted\n b\n"
	out, _ := verifyOne(t, proper, []byte("a\nb\n"))
	if string(out) != "a\ninserted\nb\n" {
		t.Fatalf("got %q", out)
	}
}

func TestNoOpPatchReportsUnchanged(t *testing.T) {
	patch := "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n b\n"
	out, st := verifyOne(t, patch, []byte("a\nb\n"))
	if string(out) != "a\nb\n" {
		t.Fatalf("got %q", out)
	}
	if st.Changed {
		t.Fatalf("expected Changed=false")
	}
}

func TestNoNLFileHunkDoesNotReachEOF(t *testing.T) {
	// git produces no marker when a hunk stops before the unterminated
	// tail line; the tail must carry its no-newline state through.
	patch := "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n-a\n+A\n b\n"
	out, _ := verifyOne(t, patch, []byte("a\nb\nc"))
	if string(out) != "A\nb\nc" {
		t.Fatalf("got %q", out)
	}
}

func TestNoNLContextTailWithMarkerGitFormat(t *testing.T) {
	// Exactly the diff "git diff" produces for a no-newline file when the
	// hunk reaches EOF: trailing context line carries the marker on both
	// sides.
	patch := "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n-x\n+X\n y\n\\ No newline at end of file\n"
	out, _ := verifyOne(t, patch, []byte("x\ny"))
	if string(out) != "X\ny" {
		t.Fatalf("got %q", out)
	}
}

func TestHunkNotAtEOFWithoutMarkerRejected(t *testing.T) {
	// Hunk covers the whole one-line file (pos == EOF) but omits the
	// no-newline marker that git itself emits.
	patch := "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-x\n+X\n"
	_, _, err := Verify(mustParse(t, patch)[0], []byte("x"))
	if err == nil {
		t.Fatalf("expected marker error (matches git apply rejection)")
	}
}

func TestNewlineFileHunkDoesNotReachEOF(t *testing.T) {
	// Terminated tail, hunk at the top: no markers required either way.
	patch := "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\n+A\n"
	out, _ := verifyOne(t, patch, []byte("a\nb\nc\n"))
	if string(out) != "A\nb\nc\n" {
		t.Fatalf("got %q", out)
	}
}

func TestModifyEmptyFileZeroZero(t *testing.T) {
	patch := "--- a/f\n+++ b/f\n@@ -0,0 +1 @@\n+only line\n"
	out, _ := verifyOne(t, patch, []byte{})
	if string(out) != "only line\n" {
		t.Fatalf("got %q", out)
	}
}

func TestHunkExtendsPastEof(t *testing.T) {
	patch := "--- a/f\n+++ b/f\n@@ -1,4 +1,4 @@\n a\n b\n-c\n+C\n d\n"
	_, _, err := Verify(mustParse(t, patch)[0], []byte("a\nb\nc\n"))
	if err == nil {
		t.Fatalf("expected past-eof error")
	}
	if ae := err.(*Error); ae.Code != CodeHunkTooLong {
		t.Fatalf("got %s", ae.Code)
	}
}

func TestDeleteMismatchRejected(t *testing.T) {
	// Well-formed deletion hunk whose removed content does not match.
	patch := "--- a/f\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-WRONG\n"
	_, _, err := Verify(mustParse(t, patch)[0], []byte("x\ny\n"))
	if err == nil {
		t.Fatalf("expected rejection")
	}
	if ae := err.(*Error); ae.Code != CodeContextMismatch {
		t.Fatalf("got %s: %v", ae.Code, err)
	}
}

func TestDeleteWrongRangeRejected(t *testing.T) {
	// Deletion hunk not covering the whole file.
	patch := "--- a/f\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n"
	_, _, err := Verify(mustParse(t, patch)[0], []byte("x\ny\n"))
	if err == nil {
		t.Fatalf("expected rejection")
	}
	if ae := err.(*Error); ae.Code != CodeLineNumberInvalid {
		t.Fatalf("got %s: %v", ae.Code, err)
	}
}
