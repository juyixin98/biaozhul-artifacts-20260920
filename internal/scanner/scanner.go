// Package scanner extracts #include directives from C-family source files.
//
// Supported subset:
//   - #include "path"  (quoted: searched relative to the including file first)
//   - #include <path>  (angled: searched in include dirs only)
//
// Comments (// and block comments) are stripped before matching, so
// pseudo-includes inside comments are ignored. Macro-generated includes
// (#include MACRO) are explicitly unsupported and reported as an error.
package scanner

import (
	"errors"
	"fmt"
	"regexp"
	"strings"
)

// Include is a single #include directive found in a source file.
type Include struct {
	Path  string `json:"path"`  // path as written in the directive
	Angle bool   `json:"angle"` // true for <...>, false for "..."
	Line  int    `json:"line"`  // 1-based line number
}

// ErrMacroInclude marks macro-generated includes, which are not supported.
var ErrMacroInclude = errors.New("macro-generated #include is not supported")

var (
	directiveRe = regexp.MustCompile(`^\s*#\s*include\s+(\S.*)$`)
	quotedRe    = regexp.MustCompile(`^"([^"]+)"$`)
	angledRe    = regexp.MustCompile(`^<([^>]+)>$`)
)

// Scan parses src and returns its include directives in source order.
// It returns an error wrapping ErrMacroInclude for any #include whose
// operand is neither "..." nor <...>.
func Scan(src string) ([]Include, error) {
	clean := stripComments(src)
	var out []Include
	for i, line := range strings.Split(clean, "\n") {
		m := directiveRe.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		operand := strings.TrimSpace(m[1])
		if q := quotedRe.FindStringSubmatch(operand); q != nil {
			out = append(out, Include{Path: q[1], Line: i + 1})
			continue
		}
		if a := angledRe.FindStringSubmatch(operand); a != nil {
			out = append(out, Include{Path: a[1], Angle: true, Line: i + 1})
			continue
		}
		return nil, fmt.Errorf("line %d: %w: %q", i+1, ErrMacroInclude, operand)
	}
	return out, nil
}

// stripComments replaces the contents of // and block comments with spaces,
// preserving newlines so line numbers stay stable. String literals are kept
// verbatim so a '"' inside a string does not confuse comment tracking.
func stripComments(src string) string {
	var b strings.Builder
	b.Grow(len(src))
	i, n := 0, len(src)
	for i < n {
		c := src[i]
		switch {
		case c == '/' && i+1 < n && src[i+1] == '/':
			for i < n && src[i] != '\n' {
				b.WriteByte(' ')
				i++
			}
		case c == '/' && i+1 < n && src[i+1] == '*':
			b.WriteString("  ")
			i += 2
			for i < n && !(src[i] == '*' && i+1 < n && src[i+1] == '/') {
				if src[i] == '\n' {
					b.WriteByte('\n')
				} else {
					b.WriteByte(' ')
				}
				i++
			}
			if i < n {
				b.WriteString("  ")
				i += 2
			}
		case c == '"':
			b.WriteByte(c)
			i++
			for i < n && src[i] != '"' {
				if src[i] == '\\' && i+1 < n {
					b.WriteString(src[i : i+2])
					i += 2
					continue
				}
				b.WriteByte(src[i])
				i++
			}
			if i < n {
				b.WriteByte(src[i])
				i++
			}
		default:
			b.WriteByte(c)
			i++
		}
	}
	return b.String()
}
