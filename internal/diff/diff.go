// Package diff parses a strict, unified-diff subset.
//
// Supported subset:
//
//	diff --git a/path b/path        (optional)
//	index <hash>..<hash> <mode>      (optional, ignored)
//	new file mode <mode>             (optional)
//	deleted file mode <mode>         (optional)
//	--- a/path | /dev/null
//	+++ b/path | /dev/null
//	@@ -oldStart,oldLines +newStart,newLines @@ optional section title
//	 context line / +added line / -removed line
//	\ No newline at end of file
//
// Multiple file sections may appear in one document. Renames, mode-only
// changes and binary diffs are explicitly rejected. Parsing never guesses:
// any malformed construct returns an Error carrying the diff line number.
package diff

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// Error codes emitted by this package.
const (
	CodeMalformedDiff     = "malformed_diff"
	CodeBadHunkHeader     = "bad_hunk_header"
	CodeCountMismatch     = "hunk_count_mismatch"
	CodeBinaryUnsupported = "binary_diff_unsupported"
	CodeUnsupportedRename = "unsupported_rename"
	CodeInvalidNoNLMarker = "invalid_no_newline_marker"
	CodeEmptyPatch        = "empty_patch"
)

// Error is a parse error located at a specific diff line.
type Error struct {
	Code    string
	Message string
	Line    int // 1-indexed line in the diff text, 0 when not applicable
	File    string
}

func (e *Error) Error() string {
	if e.Line > 0 {
		return fmt.Sprintf("%s: line %d: %s", e.Code, e.Line, e.Message)
	}
	return fmt.Sprintf("%s: %s", e.Code, e.Message)
}

// BodyLine is one line inside a hunk body. Kind is one of:
//
//	' ' context, '+' added, '-' removed, '\\' no-newline marker
type BodyLine struct {
	Kind   byte
	Text   []byte // line content without the leading prefix; empty for markers
	LineNo int    // 1-indexed line in the diff document
}

// Hunk is one @@ hunk.
type Hunk struct {
	OldStart int
	OldLines int
	NewStart int
	NewLines int
	Header   int // diff line number of the @@ header
	Body     []BodyLine
}

// FilePatch is one file section.
type FilePatch struct {
	OldPath  string // empty when OldNull
	NewPath  string // empty when NewNull
	OldNull  bool   // --- /dev/null (file is created)
	NewNull  bool   // +++ /dev/null (file is deleted)
	Hunks    []*Hunk
	HeaderAt int // diff line number of the --- header
}

// IsCreate reports whether the section creates a new file.
func (f *FilePatch) IsCreate() bool { return f.OldNull && !f.NewNull }

// IsDelete reports whether the section deletes an existing file.
func (f *FilePatch) IsDelete() bool { return !f.OldNull && f.NewNull }

// Target returns the path the section operates on.
func (f *FilePatch) Target() string {
	if f.NewNull {
		return f.OldPath
	}
	return f.NewPath
}

var hunkRe = regexp.MustCompile(`^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$`)

// Parse parses one or more file sections.
func Parse(text []byte) ([]*FilePatch, error) {
	var patches []*FilePatch
	var cur *FilePatch
	var hunk *Hunk
	oldCount, newCount := 0, 0

	finishHunk := func() error {
		if hunk == nil {
			return nil
		}
		if oldCount != hunk.OldLines {
			return &Error{Code: CodeCountMismatch, Line: hunk.Header,
				Message: fmt.Sprintf("hunk header claims %d old line(s) but body contains %d", hunk.OldLines, oldCount)}
		}
		if newCount != hunk.NewLines {
			return &Error{Code: CodeCountMismatch, Line: hunk.Header,
				Message: fmt.Sprintf("hunk header claims %d new line(s) but body contains %d", hunk.NewLines, newCount)}
		}
		cur.Hunks = append(cur.Hunks, hunk)
		hunk = nil
		return nil
	}

	finishSection := func() error {
		if err := finishHunk(); err != nil {
			return err
		}
		if cur == nil {
			return nil
		}
		if cur.OldPath == "" && !cur.OldNull && cur.NewPath == "" && !cur.NewNull && len(cur.Hunks) == 0 {
			// Empty placeholder left by a "diff --git" header; drop it.
			cur = nil
			return nil
		}
		if cur.OldPath == "" && !cur.OldNull {
			return &Error{Code: CodeMalformedDiff, Line: cur.HeaderAt, Message: "missing \"---\" header path"}
		}
		if cur.NewPath == "" && !cur.NewNull {
			return &Error{Code: CodeMalformedDiff, Line: cur.HeaderAt, Message: "missing \"+++\" header path"}
		}
		if !cur.OldNull && !cur.NewNull && cur.OldPath != cur.NewPath {
			return &Error{Code: CodeUnsupportedRename, Line: cur.HeaderAt,
				Message: fmt.Sprintf("rename %q -> %q is not supported", cur.OldPath, cur.NewPath)}
		}
		if len(cur.Hunks) == 0 && !cur.OldNull && !cur.NewNull {
			return &Error{Code: CodeMalformedDiff, Line: cur.HeaderAt,
				Message: "section contains no hunks and does not create or delete a file"}
		}
		patches = append(patches, cur)
		cur = nil
		return nil
	}

	inHunk := func() bool { return hunk != nil }

	sc := bufio.NewScanner(bytes.NewReader(text))
	sc.Buffer(make([]byte, 0, 64*1024), 32*1024*1024)
	lineNo := 0
	prevDataKind := byte(0) // kind of previous body data line, for marker validation

	for sc.Scan() {
		line := sc.Bytes()
		lineNo++

		// Reject binary patches outright.
		if bytes.Equal(line, []byte("GIT binary patch")) ||
			bytes.HasPrefix(line, []byte("Binary files ")) ||
			bytes.HasPrefix(line, []byte("Binary ")) {
			return nil, &Error{Code: CodeBinaryUnsupported, Line: lineNo, Message: "binary diffs are not supported"}
		}

		if inHunk() {
			// Once the body has supplied every declared old/new line, the
			// hunk is complete except for an optional trailing "\ No
			// newline" marker. Anything else starts (or separates) sections.
			full := oldCount == hunk.OldLines && newCount == hunk.NewLines
			if full && !(len(line) > 0 && line[0] == '\\') {
				if err := finishHunk(); err != nil {
					return nil, err
				}
			}
		}

		if inHunk() {
			switch {
			case len(line) == 0:
				// Blank line terminates a hunk body. (A blank content line
				// would appear as a single space in a unified diff.)
				if err := finishHunk(); err != nil {
					return nil, err
				}
			case line[0] == ' ' || line[0] == '+' || line[0] == '-':
				kind := line[0]
				hunk.Body = append(hunk.Body, BodyLine{
					Kind: kind, Text: append([]byte(nil), line[1:]...), LineNo: lineNo,
				})
				if kind == '+' {
					newCount++
				} else if kind == '-' {
					oldCount++
				} else {
					oldCount++
					newCount++
				}
				prevDataKind = kind
				continue
			case line[0] == '\\':
				if prevDataKind == 0 {
					return nil, &Error{Code: CodeInvalidNoNLMarker, Line: lineNo,
						Message: `"No newline at end of file" marker does not follow a content line`}
				}
				// Require the conventional text rather than accepting any
				// arbitrary backslash line as a marker.
				rest := strings.TrimPrefix(string(line), `\`)
				rest = strings.TrimPrefix(rest, " ")
				if rest != "No newline at end of file" {
					return nil, &Error{Code: CodeInvalidNoNLMarker, Line: lineNo,
						Message: "unrecognized backslash line; expected \"No newline at end of file\""}
				}
				hunk.Body = append(hunk.Body, BodyLine{Kind: '\\', LineNo: lineNo})
				prevDataKind = 0 // a marker may not be marked by another marker
				continue
			default:
				// End of hunk body: finalize and reprocess this line below.
				if err := finishHunk(); err != nil {
					return nil, err
				}
			}
		}

		// Section/header lines (not inside a hunk).
		switch {
		case bytes.HasPrefix(line, []byte("diff --git ")):
			if err := finishSection(); err != nil {
				return nil, err
			}
			cur = &FilePatch{}
			prevDataKind = 0
		case bytes.HasPrefix(line, []byte("--- ")):
			// A "---" header starts a (new) section even without a
			// preceding "diff --git" line (plain unified-diff concat).
			if err := finishSection(); err != nil {
				return nil, err
			}
			cur = &FilePatch{}
			p, null, err := parseHeaderPath(line, '-')
			if err != nil {
				return nil, &Error{Code: CodeMalformedDiff, Line: lineNo, Message: err.Error()}
			}
			cur.OldPath, cur.OldNull, cur.HeaderAt = p, null, lineNo
			prevDataKind = 0
		case bytes.HasPrefix(line, []byte("+++ ")):
			if cur == nil {
				return nil, &Error{Code: CodeMalformedDiff, Line: lineNo, Message: "\"+++\" header without a \"---\" header"}
			}
			p, null, err := parseHeaderPath(line, '+')
			if err != nil {
				return nil, &Error{Code: CodeMalformedDiff, Line: lineNo, Message: err.Error()}
			}
			cur.NewPath, cur.NewNull = p, null
		case bytes.HasPrefix(line, []byte("@@")):
			if cur == nil || (cur.OldPath == "" && !cur.OldNull) || (cur.NewPath == "" && !cur.NewNull) {
				return nil, &Error{Code: CodeMalformedDiff, Line: lineNo, Message: "hunk outside of a file section"}
			}
			m := hunkRe.FindSubmatch(line)
			if m == nil {
				return nil, &Error{Code: CodeBadHunkHeader, Line: lineNo, Message: "malformed hunk header"}
			}
			h := &Hunk{Header: lineNo}
			h.OldStart = atoi(m[1])
			h.OldLines = atoiDefault(m[2], 1)
			h.NewStart = atoi(m[3])
			h.NewLines = atoiDefault(m[4], 1)
			hunk = h
			oldCount, newCount = 0, 0
			prevDataKind = 0
		case bytes.HasPrefix(line, []byte("index ")),
			bytes.HasPrefix(line, []byte("new file mode ")),
			bytes.HasPrefix(line, []byte("deleted file mode ")),
			bytes.HasPrefix(line, []byte("old mode ")),
			bytes.HasPrefix(line, []byte("new mode ")),
			bytes.HasPrefix(line, []byte("similarity index ")),
			bytes.HasPrefix(line, []byte("dissimilarity index ")),
			bytes.HasPrefix(line, []byte("rename from ")),
			bytes.HasPrefix(line, []byte("rename to ")),
			bytes.HasPrefix(line, []byte("copy from ")),
			bytes.HasPrefix(line, []byte("copy to ")),
			bytes.HasPrefix(line, []byte("---")),
			bytes.HasPrefix(line, []byte("+++")),
			len(line) == 0:
			// tolerated metadata / blank line between sections
			if (bytes.HasPrefix(line, []byte("---")) || bytes.HasPrefix(line, []byte("+++"))) &&
				!bytes.HasPrefix(line, []byte("--- ")) && !bytes.HasPrefix(line, []byte("+++ ")) {
				return nil, &Error{Code: CodeMalformedDiff, Line: lineNo, Message: "malformed file header (missing space after marker)"}
			}
		default:
			return nil, &Error{Code: CodeMalformedDiff, Line: lineNo,
				Message: fmt.Sprintf("unexpected line %q outside hunk body", truncate(line, 80))}
		}
	}
	if err := sc.Err(); err != nil {
		return nil, &Error{Code: CodeMalformedDiff, Message: err.Error()}
	}
	if err := finishSection(); err != nil {
		return nil, err
	}
	if len(patches) == 0 {
		return nil, &Error{Code: CodeEmptyPatch, Message: "patch contains no file sections"}
	}
	return patches, nil
}

var (
	tabDateRe  = regexp.MustCompile(`\t[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?: [+-][0-9]{4}| Z)?$`)
	tabEpochRe = regexp.MustCompile(`\t[0-9]{9,}(?:\.[0-9]+)?(?: -?[0-9]+)?$`)
)

func parseHeaderPath(line []byte, marker byte) (string, bool, error) {
	s := string(line[4:])
	if s == "/dev/null" {
		return "", true, nil
	}
	// Strip the tab + timestamp suffix used by git/diff.
	if i := strings.IndexByte(s, '\t'); i >= 0 {
		if tabDateRe.MatchString(s) || tabEpochRe.MatchString(s) {
			s = s[:i]
		}
	}
	if strings.ContainsRune(s, 0) {
		return "", false, errors.New("NUL byte in header path")
	}
	// Strip the conventional a/ b/ prefix pair. Only strip when the
	// content looks like a prefixed path ("a/..." or "b/...").
	if (marker == '-' && strings.HasPrefix(s, "a/")) ||
		(marker == '+' && strings.HasPrefix(s, "b/")) {
		s = s[2:]
	}
	if s == "" {
		return "", false, errors.New("empty path in file header")
	}
	return s, false, nil
}

func atoi(b []byte) int {
	n, _ := strconv.Atoi(string(b))
	return n
}

func atoiDefault(b []byte, def int) int {
	if len(b) == 0 {
		return def
	}
	return atoi(b)
}

func truncate(b []byte, n int) string {
	if len(b) <= n {
		return string(b)
	}
	return string(b[:n]) + "..."
}
