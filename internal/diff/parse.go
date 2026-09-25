// Package diff parses a small, strictly interpreted subset of the unified diff
// format. There is no fuzziness: every hunk header, every line count and every
// "\ No newline at end of file" marker must be consistent with the text that
// follows it, otherwise Parse returns an error.
//
// Supported subset:
//
//   - "diff --git a/x b/x", "index", "new file mode", "deleted file mode"
//     and similar metadata lines are accepted and ignored;
//   - "--- a/path" / "+++ b/path" file headers (tab-separated timestamps
//     are stripped); "/dev/null" marks a new or deleted file;
//   - "@@ -oldStart,oldCount +newStart,newCount @@" hunk headers;
//   - hunk lines beginning with ' ' (context), '-' (removed) and '+' (added);
//   - "\ No newline at end of file" markers.
//
// Not supported (and rejected): C-style quoted paths and any hunk line that
// does not start with one of the four markers above.
package diff

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// LineKind is the kind of a hunk body line.
type LineKind byte

const (
	Context LineKind = ' '
	Delete  LineKind = '-'
	Add     LineKind = '+'
)

// Line is one body line of a hunk. Text does not include the leading kind
// character nor the trailing newline.
type Line struct {
	Kind LineKind
	Text string
}

// Hunk is one "@@" section. OldNoNewline/NewNoNewline record whether the last
// old/new side line carries a "\ No newline at end of file" marker.
type Hunk struct {
	OldStart     int
	OldCount     int
	NewStart     int
	NewCount     int
	Lines        []Line
	OldNoNewline bool
	NewNoNewline bool
}

// FilePatch is the patch for one file. OldPath == "" means the old side is
// "/dev/null" (a new file); NewPath == "" means the new side is "/dev/null"
// (a deletion).
type FilePatch struct {
	OldPath string
	NewPath string
	Hunks   []Hunk
}

// IsNew reports whether the patch creates the file.
func (f FilePatch) IsNew() bool { return f.OldPath == "" }

// IsDelete reports whether the patch deletes the file.
func (f FilePatch) IsDelete() bool { return f.NewPath == "" }

var hunkRe = regexp.MustCompile(`^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:\s.*)?$`)

// Parse parses one patch document containing one or more file patches.
func Parse(text string) ([]FilePatch, error) {
	lines := strings.Split(text, "\n")
	// Split always yields a final "" for a document ending with "\n".
	if n := len(lines); n > 0 && lines[n-1] == "" {
		lines = lines[:n-1]
	}

	var patches []FilePatch
	i := 0
	for i < len(lines) {
		if !strings.HasPrefix(lines[i], "--- ") {
			// Skip metadata lines ("diff --git", "index", "* file mode",
			// blank separators, ...). Anything else is caught later when a
			// hunk body expects specific line prefixes.
			i++
			continue
		}

		fp := FilePatch{}
		oldPath, err := parseHeaderPath(strings.TrimPrefix(lines[i], "--- "))
		if err != nil {
			return nil, fmt.Errorf("line %d: %w", i+1, err)
		}
		i++
		if i >= len(lines) || !strings.HasPrefix(lines[i], "+++ ") {
			return nil, fmt.Errorf("line %d: expected \"+++ \" header after \"--- \"", i+1)
		}
		newPath, err := parseHeaderPath(strings.TrimPrefix(lines[i], "+++ "))
		if err != nil {
			return nil, fmt.Errorf("line %d: %w", i+1, err)
		}
		fp.OldPath, fp.NewPath = oldPath, newPath
		i++

		if fp.OldPath == "" && fp.NewPath == "" {
			return nil, fmt.Errorf("line %d: both old and new paths are /dev/null", i)
		}

		for i < len(lines) && strings.HasPrefix(lines[i], "@@") {
			h, next, err := parseHunk(lines, i)
			if err != nil {
				return nil, err
			}
			fp.Hunks = append(fp.Hunks, h)
			i = next
		}
		if len(fp.Hunks) == 0 {
			return nil, fmt.Errorf("file %q: expected at least one hunk", headerName(fp))
		}
		patches = append(patches, fp)
	}

	if len(patches) == 0 {
		return nil, fmt.Errorf("no \"--- / +++\" file patches found")
	}
	return patches, nil
}

func headerName(fp FilePatch) string {
	if fp.NewPath != "" {
		return fp.NewPath
	}
	return fp.OldPath
}

func parseHunk(lines []string, start int) (Hunk, int, error) {
	m := hunkRe.FindStringSubmatch(lines[start])
	if m == nil {
		return Hunk{}, 0, fmt.Errorf("line %d: malformed hunk header %q", start+1, lines[start])
	}
	oldStart, _ := strconv.Atoi(m[1])
	newStart, _ := strconv.Atoi(m[3])
	oldCount, newCount := 1, 1
	if m[2] != "" {
		oldCount, _ = strconv.Atoi(m[2])
	}
	if m[4] != "" {
		newCount, _ = strconv.Atoi(m[4])
	}
	if oldStart < 0 || newStart < 0 || oldCount < 0 || newCount < 0 {
		return Hunk{}, 0, fmt.Errorf("line %d: negative hunk coordinates", start+1)
	}
	h := Hunk{OldStart: oldStart, OldCount: oldCount, NewStart: newStart, NewCount: newCount}

	oldRemain, newRemain := oldCount, newCount
	i := start + 1
	for oldRemain > 0 || newRemain > 0 {
		if i >= len(lines) {
			return Hunk{}, 0, fmt.Errorf("line %d: hunk starting on line %d is truncated", len(lines)+1, start+1)
		}
		l := lines[i]
		// A truly empty line is tolerated as an empty context line; some
		// hand-written patches omit the required leading space.
		if l == "" {
			l = " "
		}
		switch l[0] {
		case ' ':
			h.Lines = append(h.Lines, Line{Context, l[1:]})
			oldRemain--
			newRemain--
		case '-':
			h.Lines = append(h.Lines, Line{Delete, l[1:]})
			oldRemain--
		case '+':
			h.Lines = append(h.Lines, Line{Add, l[1:]})
			newRemain--
		case '\\':
			if err := markNoNewline(&h, i+1, newRemain == 0); err != nil {
				return Hunk{}, 0, err
			}
		default:
			return Hunk{}, 0, fmt.Errorf("line %d: unexpected hunk line %q", i+1, l)
		}
		if oldRemain < 0 || newRemain < 0 {
			return Hunk{}, 0, fmt.Errorf("line %d: hunk has more body lines than its header declares", i+1)
		}
		i++
	}
	// The no-newline marker follows the final body line, after the header
	// counts are already satisfied.
	if i < len(lines) && strings.HasPrefix(lines[i], "\\") {
		if err := markNoNewline(&h, i+1, true); err != nil {
			return Hunk{}, 0, err
		}
		i++
	}
	return h, i, nil
}

// markNoNewline applies a "\ No newline at end of file" marker to the previous
// hunk line. A removed line belongs to the old side and an added line to the
// new side. A context line belongs to both sides, but its marker only applies
// to the new side when no further new-side lines follow in this hunk
// (newSideDone) - otherwise the marker can only describe the old side, which
// is how git emits "append to a file without trailing newline" hunks.
func markNoNewline(h *Hunk, lineNo int, newSideDone bool) error {
	if len(h.Lines) == 0 {
		return fmt.Errorf("line %d: \"\\ No newline at end of file\" marker with no preceding line", lineNo)
	}
	switch h.Lines[len(h.Lines)-1].Kind {
	case Delete:
		h.OldNoNewline = true
	case Add:
		h.NewNoNewline = true
	case Context:
		h.OldNoNewline = true
		if newSideDone {
			h.NewNoNewline = true
		}
	}
	return nil
}

func parseHeaderPath(s string) (string, error) {
	if t := strings.IndexByte(s, '\t'); t >= 0 {
		s = s[:t]
	}
	s = strings.TrimRight(s, " \r")
	if s == "/dev/null" {
		return "", nil
	}
	if strings.HasPrefix(s, `"`) {
		return "", fmt.Errorf("quoted file names are not supported: %s", s)
	}
	if strings.HasPrefix(s, "a/") || strings.HasPrefix(s, "b/") {
		s = s[2:]
	}
	if s == "" {
		return "", fmt.Errorf("empty file name in diff header")
	}
	return s, nil
}
