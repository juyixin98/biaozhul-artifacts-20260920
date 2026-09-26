package rangespec

import (
	"net/http"
	"strings"
	"time"
)

// IfRange decides whether a conditional Range request may be served as a
// partial response.
//
// RFC 9110: If-Range carries either an entity tag or an HTTP-date.
//   - ETag form: the range is honored only when the validator exactly
//     matches the current strong validator. Weak validators ("W/") are
//     never usable for sub-range selection.
//   - Date form: the range is honored only when the representation was not
//     modified *after* the given date (the common case the client asks
//     about: a timestamp equal to Last-Modified counts as unmodified).
//
// currentLastModified may be the zero time when the representation has no
// last-modified metadata; in that case a date-valued If-Range can never be
// matched conservatively... RFC treats a matching date as a match, but with
// no modification date known we refuse the range.
func IfRange(headerVal string, currentETag string, currentLastModified time.Time) bool {
	v := strings.TrimSpace(headerVal)
	if v == "" {
		// No If-Range header: unconditional range request.
		return true
	}
	if isDateForm(v) {
		if currentLastModified.IsZero() {
			return false
		}
		t, err := http.ParseTime(v)
		if err != nil {
			// Unparseable validator: ignore the precondition by sending the
			// whole representation, i.e. do NOT honor the range.
			return false
		}
		return !currentLastModified.Truncate(time.Second).After(t.Truncate(time.Second))
	}

	// Entity-tag form. Comparison is exact, strong-vs-strong only.
	if strings.HasPrefix(v, "W/") {
		return false
	}
	return v == strings.TrimSpace(currentETag)
}

// isDateForm reports whether an If-Range value looks like an HTTP-date
// rather than an entity tag. Entity tags always start with a quote; dates
// never do.
func isDateForm(v string) bool {
	return !strings.HasPrefix(v, `"`)
}
