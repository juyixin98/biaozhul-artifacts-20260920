package kv

// glob reports whether name matches the Redis-style pattern:
//
//	'?' matches any single byte
//	'*' matches any (possibly empty) run of bytes
//	'[...]' matches one byte in a class, supporting '^'/'!' negation and '-' ranges
//	'\\' escapes the next byte literally
//
// It mirrors the small glob dialect used by the KEYS command. Matching is
// byte-wise and therefore binary safe.
func glob(pattern, name string) bool {
	return matchGlob([]byte(pattern), []byte(name))
}

func matchGlob(p, s []byte) bool {
	for len(p) > 0 {
		switch p[0] {
		case '*':
			// Collapse consecutive stars.
			for len(p) > 1 && p[1] == '*' {
				p = p[1:]
			}
			if len(p) == 1 {
				return true // trailing star matches everything
			}
			// Try matching the remainder at every position.
			for i := 0; i <= len(s); i++ {
				if matchGlob(p[1:], s[i:]) {
					return true
				}
			}
			return false
		case '?':
			if len(s) == 0 {
				return false
			}
			s = s[1:]
		case '[':
			if len(s) == 0 {
				return false
			}
			consumed, ok := matchClass(p, s[0])
			if !ok {
				return false
			}
			p = p[consumed:]
			s = s[1:]
			continue
		case '\\':
			if len(p) < 2 {
				return false // dangling escape is a malformed pattern
			}
			if len(s) == 0 || s[0] != p[1] {
				return false
			}
			p = p[2:]
			s = s[1:]
			continue
		default:
			if len(s) == 0 || s[0] != p[0] {
				return false
			}
			s = s[1:]
		}
		p = p[1:]
	}
	return len(s) == 0
}

// matchClass evaluates a [...] class starting at p[0] against byte c. It
// returns the number of pattern bytes the class occupies and whether c
// matched.
func matchClass(p []byte, c byte) (int, bool) {
	// p[0] == '['
	negate := false
	i := 1
	if i < len(p) && (p[i] == '^' || p[i] == '!') {
		negate = true
		i++
	}
	matched := false
	// A leading ']' is a literal member in Redis globs.
	if i < len(p) && p[i] == ']' {
		if c == ']' {
			matched = true
		}
		i++
	}
	for i < len(p) && p[i] != ']' {
		lo := p[i]
		if lo == '\\' && i+1 < len(p) {
			i++
			lo = p[i]
		}
		i++
		hi := lo
		if i+1 < len(p) && p[i] == '-' && p[i+1] != ']' {
			i++ // skip '-'
			hi = p[i]
			if hi == '\\' && i+1 < len(p) {
				i++
				hi = p[i]
			}
			i++
		}
		if c >= lo && c <= hi {
			matched = true
		}
	}
	if i >= len(p) {
		return 0, false // unterminated class
	}
	i++ // consume ']'
	if negate {
		matched = !matched
	}
	return i, matched
}
