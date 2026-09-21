package glob

import (
	"regexp"
	"strings"
)

// Match reports whether name matches a case-insensitive local wildcard
// pattern. '*' matches any run of characters; everything else is literal.
func Match(pattern, name string) (bool, error) {
	re, err := Compile(pattern)
	if err != nil {
		return false, err
	}
	return re.MatchString(strings.ToLower(name)), nil
}

// Compile builds the anchored case-insensitive regexp for a glob pattern.
func Compile(glob string) (*regexp.Regexp, error) {
	glob = strings.ToLower(glob)
	var b strings.Builder
	b.WriteString(`(?s)\A`)
	for _, r := range glob {
		if r == '*' {
			b.WriteString(".*")
		} else {
			b.WriteString(regexp.QuoteMeta(string(r)))
		}
	}
	b.WriteString(`\z`)
	return regexp.Compile(b.String())
}
