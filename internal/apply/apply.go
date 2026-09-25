// Package apply applies parsed unified-diff file patches exactly, with no
// fuzzy matching. Every context and removed line must byte-equal the target
// file content at the position declared by the hunk header; hunk ranges may
// not overlap; and "no newline at end of file" markers are enforced.
package apply

import (
	"fmt"

	"patchd/internal/diff"
)

const (
	CodePathNotFound      = "path_not_found"
	CodeAlreadyExists     = "already_exists"
	CodeContextMismatch   = "context_mismatch"
	CodeLineNumberInvalid = "line_number_invalid"
	CodeOverlappingHunks  = "overlapping_hunks"
	CodeNewStartMismatch  = "new_start_mismatch"
	CodeMarkerInvalid     = "no_newline_marker_invalid"
	CodeHunkTooLong       = "hunk_extends_past_eof"
)

// Error is an application/validation error for one file patch.
type Error struct {
	Code    string
	Path    string
	Hunk    int // 1-based hunk index, 0 when not hunk-specific
	Line    int // diff line number when available
	Message string
}

func (e *Error) Error() string {
	if e.Hunk > 0 {
		return fmt.Sprintf("%s: %s: hunk %d: %s", e.Code, e.Path, e.Hunk, e.Message)
	}
	return fmt.Sprintf("%s: %s: %s", e.Code, e.Path, e.Message)
}

// Stats summarizes a successful application.
type Stats struct {
	Hunks     int
	Added     int
	Removed   int
	Context   int
	Changed   bool // false when the patched content is identical to the source
	Existed   bool // the file existed before
	WillExist bool // the file exists after (false for deletions)
}

// seg is one line segment. noNL marks the final, unterminated line.
type seg struct {
	data []byte
	noNL bool
}

func splitLines(data []byte) []seg {
	if len(data) == 0 {
		return nil
	}
	var out []seg
	for {
		i := indexByte(data, '\n')
		if i < 0 {
			out = append(out, seg{data: data, noNL: true})
			return out
		}
		out = append(out, seg{data: data[:i]})
		data = data[i+1:]
		if len(data) == 0 {
			return out
		}
	}
}

func indexByte(b []byte, c byte) int {
	for i := 0; i < len(b); i++ {
		if b[i] == c {
			return i
		}
	}
	return -1
}

func joinLines(segs []seg) []byte {
	var buf []byte
	for _, s := range segs {
		buf = append(buf, s.data...)
		if !s.noNL {
			buf = append(buf, '\n')
		}
	}
	return buf
}

func bytesEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func cloneBytes(b []byte) []byte {
	c := make([]byte, len(b))
	copy(c, b)
	return c
}

// Verify computes the resulting content without touching the filesystem.
// original is nil when the target file does not exist. A nil result with nil
// error means the file is to be deleted.
func Verify(fp *diff.FilePatch, original []byte) ([]byte, Stats, error) {
	stats := Stats{Hunks: len(fp.Hunks), Existed: original != nil}

	switch {
	case fp.IsCreate():
		return verifyCreate(fp, original, stats)
	case fp.IsDelete():
		return verifyDelete(fp, original, stats)
	default:
		return verifyModify(fp, original, stats)
	}
}

func verifyCreate(fp *diff.FilePatch, original []byte, stats Stats) ([]byte, Stats, error) {
	if original != nil {
		return nil, stats, &Error{Code: CodeAlreadyExists, Path: fp.NewPath,
			Message: "patch creates a file that already exists"}
	}
	switch len(fp.Hunks) {
	case 0:
		stats.WillExist = true
		stats.Changed = true
		return []byte{}, stats, nil
	case 1:
		// expected below
	default:
		return nil, stats, &Error{Code: CodeLineNumberInvalid, Path: fp.NewPath,
			Message: "file creation must contain exactly one hunk"}
	}
	h := fp.Hunks[0]
	if h.OldStart != 0 || h.OldLines != 0 {
		return nil, stats, &Error{Code: CodeLineNumberInvalid, Path: fp.NewPath, Hunk: 1, Line: h.Header,
			Message: `file creation hunk must start with "-0,0"`}
	}
	if h.NewLines == 0 {
		return nil, stats, &Error{Code: CodeLineNumberInvalid, Path: fp.NewPath, Hunk: 1, Line: h.Header,
			Message: "file creation hunk contains no added lines"}
	}
	if h.NewStart != 1 {
		return nil, stats, &Error{Code: CodeNewStartMismatch, Path: fp.NewPath, Hunk: 1, Line: h.Header,
			Message: fmt.Sprintf("file creation hunk should start at new line 1, declares %d", h.NewStart)}
	}
	bp, err := processBody(fp, h, nil, true)
	if err != nil {
		return nil, stats, err
	}
	stats.Added, stats.Context = bp.added, bp.context
	stats.Changed = true
	stats.WillExist = true
	result := joinLines(bp.out)
	if err := checkNoNLOnlyAtEnd(fp, bp.out); err != nil {
		return nil, stats, err
	}
	return result, stats, nil
}

func verifyDelete(fp *diff.FilePatch, original []byte, stats Stats) ([]byte, Stats, error) {
	if original == nil {
		return nil, stats, &Error{Code: CodePathNotFound, Path: fp.OldPath,
			Message: "patch deletes a file that does not exist"}
	}
	src := splitLines(original)
	if len(fp.Hunks) == 0 {
		if len(src) != 0 {
			return nil, stats, &Error{Code: CodeContextMismatch, Path: fp.OldPath,
				Message: "empty deletion patch but the file is non-empty"}
		}
		stats.Changed = true
		return nil, stats, nil
	}
	if len(fp.Hunks) != 1 {
		return nil, stats, &Error{Code: CodeLineNumberInvalid, Path: fp.OldPath,
			Message: "file deletion must contain exactly one hunk covering the whole file"}
	}
	h := fp.Hunks[0]
	if h.OldStart != 1 || h.OldLines != len(src) {
		return nil, stats, &Error{Code: CodeLineNumberInvalid, Path: fp.OldPath, Hunk: 1, Line: h.Header,
			Message: fmt.Sprintf("deletion hunk must cover lines 1-%d, declares %d-%d",
				len(src), h.OldStart, h.OldStart+h.OldLines-1)}
	}
	if h.NewStart != 0 || h.NewLines != 0 {
		return nil, stats, &Error{Code: CodeNewStartMismatch, Path: fp.OldPath, Hunk: 1, Line: h.Header,
			Message: `deletion hunk must produce zero new lines ("+0,0")`}
	}
	bp, err := processBody(fp, h, src, false)
	if err != nil {
		return nil, stats, err
	}
	if srcNoNL := src[len(src)-1].noNL; srcNoNL != bp.oldMarked {
		if srcNoNL {
			return nil, stats, &Error{Code: CodeMarkerInvalid, Path: fp.OldPath, Hunk: 1,
				Message: `the deleted file's last line has no trailing newline but the patch does not mark it`}
		}
		return nil, stats, &Error{Code: CodeMarkerInvalid, Path: fp.OldPath, Hunk: 1,
			Message: `the patch marks "No newline at end of file" but the deleted file ends with a newline`}
	}
	if len(bp.out) != 0 || bp.added != 0 {
		return nil, stats, &Error{Code: CodeContextMismatch, Path: fp.OldPath, Hunk: 1,
			Message: "deletion patch leaves content in the file"}
	}
	stats.Removed, stats.Context = bp.removed, bp.context
	stats.Changed = true
	return nil, stats, nil
}

func verifyModify(fp *diff.FilePatch, original []byte, stats Stats) ([]byte, Stats, error) {
	if original == nil {
		return nil, stats, &Error{Code: CodePathNotFound, Path: fp.OldPath,
			Message: "patch modifies a file that does not exist"}
	}
	src := splitLines(original)
	var out []seg
	pos := 0 // next unconsumed index in src
	curNew := 1
	var agg bodyAgg

	for hi, h := range fp.Hunks {
		hunkNo := hi + 1

		var startIdx int
		switch {
		case h.OldLines == 0:
			// Pure insertion after line OldStart (0 = before line 1).
			startIdx = h.OldStart
			if startIdx < 0 || startIdx > len(src) {
				return nil, stats, &Error{Code: CodeHunkTooLong, Path: fp.Target(),
					Hunk: hunkNo, Line: h.Header,
					Message: fmt.Sprintf("insertion point line %d is outside the file (%d line(s))", h.OldStart, len(src))}
			}
		default:
			if h.OldStart < 1 {
				return nil, stats, &Error{Code: CodeLineNumberInvalid, Path: fp.Target(),
					Hunk: hunkNo, Line: h.Header, Message: "hunk with content must start at line 1 or later"}
			}
			startIdx = h.OldStart - 1
			if startIdx+h.OldLines > len(src) {
				return nil, stats, &Error{Code: CodeHunkTooLong, Path: fp.Target(),
					Hunk: hunkNo, Line: h.Header,
					Message: fmt.Sprintf("hunk covers lines %d-%d but file has %d line(s)",
						h.OldStart, h.OldStart+h.OldLines-1, len(src))}
			}
		}
		if startIdx < pos {
			return nil, stats, &Error{Code: CodeOverlappingHunks, Path: fp.Target(),
				Hunk: hunkNo, Line: h.Header,
				Message: fmt.Sprintf("hunk starting at line %d overlaps the previous hunk", h.OldStart)}
		}

		// Unchanged gap before the hunk. A marker in the previous hunk
		// marked EOF, so nothing (old or new) may follow it. The gap also
		// advances the expected new-side line counter.
		gap := src[pos:startIdx]
		if len(gap) > 0 && (agg.oldMarked || agg.newMarked) {
			return nil, stats, &Error{Code: CodeMarkerInvalid, Path: fp.Target(), Hunk: hunkNo,
				Line: h.Header, Message: `"No newline at end of file" marker is not at the end of the file`}
		}
		curNew += len(gap)
		if h.NewStart != curNew {
			return nil, stats, &Error{Code: CodeNewStartMismatch, Path: fp.Target(),
				Hunk: hunkNo, Line: h.Header,
				Message: fmt.Sprintf("hunk declares new start line %d but expected %d", h.NewStart, curNew)}
		}
		out = append(out, gap...)

		bp, err := processBody(fp, h, src[startIdx:startIdx+h.OldLines], false)
		if err != nil {
			return nil, stats, err
		}
		out = append(out, bp.out...)
		agg.added += bp.added
		agg.removed += bp.removed
		agg.context += bp.context
		agg.oldMarked = agg.oldMarked || bp.oldMarked
		agg.newMarked = agg.newMarked || bp.newMarked

		pos = startIdx + h.OldLines
		curNew = h.NewStart + h.NewLines
	}

	// Unchanged tail.
	tail := src[pos:]
	if len(tail) > 0 && (agg.oldMarked || agg.newMarked) {
		return nil, stats, &Error{Code: CodeMarkerInvalid, Path: fp.Target(),
			Message: `"No newline at end of file" marker is followed by unchanged lines`}
	}
	out = append(out, tail...)

	// Cross-check EOF newline state only on the side whose end the hunks
	// actually reach. When an unchanged tail remains, its segments carry
	// the source newline state through unchanged, so no marker is needed.
	if pos == len(src) {
		srcNoNL := len(src) > 0 && src[len(src)-1].noNL
		if srcNoNL != agg.oldMarked {
			if srcNoNL {
				return nil, stats, &Error{Code: CodeMarkerInvalid, Path: fp.Target(),
					Message: `the file's last line has no trailing newline but the patch does not mark it ("No newline at end of file")`}
			}
			return nil, stats, &Error{Code: CodeMarkerInvalid, Path: fp.Target(),
				Message: `the patch marks "No newline at end of file" but the old file ends with a newline`}
		}
	}
	if len(tail) == 0 {
		newNoNL := len(out) > 0 && out[len(out)-1].noNL
		if newNoNL != agg.newMarked {
			if newNoNL {
				return nil, stats, &Error{Code: CodeMarkerInvalid, Path: fp.Target(),
					Message: `the result's last line has no trailing newline but the patch does not mark it`}
			}
			return nil, stats, &Error{Code: CodeMarkerInvalid, Path: fp.Target(),
				Message: `the patch marks "No newline at end of file" on the new side but the result ends with a newline`}
		}
	}

	stats.Added, stats.Removed, stats.Context = agg.added, agg.removed, agg.context
	stats.WillExist = true
	result := joinLines(out)
	if err := checkNoNLOnlyAtEnd(fp, out); err != nil {
		return nil, stats, err
	}
	stats.Changed = !bytesEqual(result, original)
	return result, stats, nil
}

// checkNoNLOnlyAtEnd enforces that only the final line segment may be
// unterminated — a "no newline" marker anywhere else is invalid.
func checkNoNLOnlyAtEnd(fp *diff.FilePatch, out []seg) error {
	for i := 0; i+1 < len(out); i++ {
		if out[i].noNL {
			return &Error{Code: CodeMarkerInvalid, Path: fp.Target(),
				Message: `"No newline at end of file" marker appears before the end of the file`}
		}
	}
	return nil
}

type bodyAgg struct {
	added                int
	removed              int
	context              int
	oldMarked, newMarked bool
}

type bodyProc struct {
	out                     []seg
	added, removed, context int
	oldMarked, newMarked    bool
}

// processBody verifies one hunk body against the consumed source segments.
// For creation hunks consumed is nil and createMode is true: only added lines
// are legal.
func processBody(fp *diff.FilePatch, h *diff.Hunk, consumed []seg, createMode bool) (bodyProc, error) {
	var bp bodyProc
	oldPos := 0
	var lastKind byte
	var lastConsumedNoNL bool

	hunkNo := hunkIndex(fp, h)
	errAt := func(lineNo int, code, msg string) *Error {
		return &Error{Code: code, Path: fp.Target(), Hunk: hunkNo, Line: lineNo, Message: msg}
	}

	for _, bl := range h.Body {
		switch bl.Kind {
		case ' ':
			if createMode {
				return bp, errAt(bl.LineNo, CodeContextMismatch, "creation hunk contains a context line")
			}
			if bp.oldMarked {
				return bp, errAt(bl.LineNo, CodeMarkerInvalid,
					`line follows a "No newline at end of file" marker on the old side`)
			}
			if bp.newMarked {
				return bp, errAt(bl.LineNo, CodeMarkerInvalid,
					`line follows a "No newline at end of file" marker on the new side`)
			}
			if oldPos >= len(consumed) {
				return bp, errAt(bl.LineNo, CodeContextMismatch,
					"hunk body contains more old-side lines than its header declares")
			}
			want := consumed[oldPos]
			if !bytesEqual(want.data, bl.Text) {
				return bp, errAt(bl.LineNo, CodeContextMismatch,
					fmt.Sprintf("context mismatch at file line %d", h.OldStart+oldPos))
			}
			bp.out = append(bp.out, seg{data: cloneBytes(bl.Text)})
			lastConsumedNoNL = want.noNL
			oldPos++
			bp.context++
			lastKind = ' '
		case '-':
			if createMode {
				return bp, errAt(bl.LineNo, CodeContextMismatch, "creation hunk removes a line")
			}
			if bp.oldMarked {
				return bp, errAt(bl.LineNo, CodeMarkerInvalid,
					`line follows a "No newline at end of file" marker on the old side`)
			}
			if oldPos >= len(consumed) {
				return bp, errAt(bl.LineNo, CodeContextMismatch,
					"hunk body removes more lines than its header declares")
			}
			want := consumed[oldPos]
			if !bytesEqual(want.data, bl.Text) {
				return bp, errAt(bl.LineNo, CodeContextMismatch,
					fmt.Sprintf("removed line mismatch at file line %d", h.OldStart+oldPos))
			}
			lastConsumedNoNL = want.noNL
			oldPos++
			bp.removed++
			lastKind = '-'
		case '+':
			if bp.newMarked {
				return bp, errAt(bl.LineNo, CodeMarkerInvalid,
					`added line follows a "No newline at end of file" marker`)
			}
			bp.out = append(bp.out, seg{data: cloneBytes(bl.Text)})
			bp.added++
			lastKind = '+'
		case '\\':
			switch lastKind {
			case ' ':
				if !lastConsumedNoNL {
					return bp, errAt(bl.LineNo, CodeMarkerInvalid,
						`patch marks "No newline at end of file" but that context line ends with a newline`)
				}
				bp.out[len(bp.out)-1].noNL = true
				bp.oldMarked = true
				bp.newMarked = true
			case '-':
				if !lastConsumedNoNL {
					return bp, errAt(bl.LineNo, CodeMarkerInvalid,
						`patch marks "No newline at end of file" but that removed line ends with a newline`)
				}
				bp.oldMarked = true
			case '+':
				bp.out[len(bp.out)-1].noNL = true
				bp.newMarked = true
			default:
				return bp, errAt(bl.LineNo, CodeMarkerInvalid,
					`"No newline at end of file" marker does not follow a content line`)
			}
			lastKind = '\\'
		}
	}
	if !createMode && oldPos != len(consumed) {
		return bp, errAt(h.Header, CodeContextMismatch,
			fmt.Sprintf("hunk body verifies %d of %d declared old line(s)", oldPos, len(consumed)))
	}
	return bp, nil
}

func hunkIndex(fp *diff.FilePatch, h *diff.Hunk) int {
	for i, x := range fp.Hunks {
		if x == h {
			return i + 1
		}
	}
	return 0
}
