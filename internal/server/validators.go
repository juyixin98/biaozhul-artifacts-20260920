package server

import (
	"net/http"
	"strings"
	"time"
)

// trimOWS strips surrounding spaces/tabs from a header token.
func trimOWS(s string) string {
	return strings.Trim(s, " \t")
}

// isWeakETag reports whether tag uses the W/ weak prefix.
func isWeakETag(tag string) bool {
	return strings.HasPrefix(tag, "W/") || strings.HasPrefix(tag, "w/")
}

// ifRangeAllowsRange evaluates the If-Range precondition (RFC 9110 §13.1.3).
//
// semantics:
//   - no If-Range header: ranges are allowed (no precondition to fail).
//   - entity-tag: a weak tag, or any malformed field, makes the field
//     ignorable per spec ("MUST ignore"), hence ranges stay allowed.
//     A strong tag is compared with the strong comparison function.
//   - HTTP-date: the range is allowed iff the date is not earlier than the
//     representation's Last-Modified instant.
//   - anything unparseable: the field is ignored.
func ifRangeAllowsRange(header string, rep representation, lastModified time.Time) bool {
	v := trimOWS(header)
	if v == "" {
		return true
	}
	if strings.HasPrefix(v, `"`) {
		if isWeakETag(v) {
			return true
		}
		return strongETagEqual(v, rep.etag)
	}
	if t, err := http.ParseTime(v); err == nil {
		return !t.Before(lastModified.UTC())
	}
	return true
}

// strongETagEqual implements the strong comparison (RFC 9110 §8.8.3.2):
// both tags must be non-weak and identical opaque-quoted strings. The tags
// produced by this project are always strong, so we compare verbatim.
func strongETagEqual(clientTag, currentTag string) bool {
	if isWeakETag(clientTag) || isWeakETag(currentTag) {
		return false
	}
	return clientTag == currentTag
}
