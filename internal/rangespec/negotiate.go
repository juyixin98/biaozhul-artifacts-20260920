package rangespec

import (
	"mime"
	"strconv"
	"strings"
)

// SelectEncoding performs content negotiation for the representations this
// server offers: identity and gzip.
//
// It implements the Accept-Encoding subset of RFC 9110 needed here:
//   - missing header or an unmentioned coding: acceptable at q=1
//   - "*" matches any coding not explicitly listed
//   - q=0 (or q=0.0…) means "not acceptable"
//
// gzip wins when the client explicitly lists it at a quality no lower than
// identity's; identity wins every tie otherwise, which keeps range offsets
// aligned with the canonical bytes the client verifies. The returned bool
// reports whether any offered encoding is acceptable; false means the
// handler must answer 406.
func SelectEncoding(acceptEncoding string, offersGzip bool) (encoding string, ok bool) {
	candidates := parseCandidates(acceptEncoding)

	// qOf resolves the quality for one coding: an explicit entry wins,
	// otherwise "*" applies, otherwise an unmentioned coding is acceptable.
	qOf := func(name string) (q float64, explicitlyListed bool) {
		if q, listed := candidates[name]; listed {
			return q, true
		}
		if q, listed := candidates["*"]; listed {
			return q, false
		}
		return 1.0, false
	}

	gzipQ, gzipExplicit := qOf(RepGzip)
	identityQ, _ := qOf(RepIdentity)

	if offersGzip && gzipExplicit && gzipQ > 0 && gzipQ >= identityQ {
		return RepGzip, true
	}
	if identityQ > 0 {
		return RepIdentity, true
	}
	// Identity rejected: gzip may still save the response.
	if offersGzip && gzipQ > 0 {
		return RepGzip, true
	}
	return "", false
}

func parseCandidates(h string) map[string]float64 {
	out := map[string]float64{}
	for _, part := range strings.Split(h, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		segs := strings.Split(part, ";")
		name := strings.TrimSpace(segs[0])
		q := 1.0
		for _, p := range segs[1:] {
			p = strings.TrimSpace(p)
			if strings.HasPrefix(p, "q=") {
				if v, err := strconv.ParseFloat(strings.TrimPrefix(p, "q="), 64); err == nil {
					q = v
				}
			}
		}
		out[name] = q
	}
	return out
}

// RepIdentity and RepGzip are the representation names used across
// packages; they mirror artifact.RepIdentity / artifact.RepGzip.
const (
	RepIdentity = "identity"
	RepGzip     = "gzip"
)

// Boundary generates a multipart boundary from a caller-supplied nonce
// (usually a request id) so tests are deterministic.
func Boundary(nonce string) string {
	return "rangereq_" + strings.Trim(mime.QEncoding.Encode("utf-8", nonce), `"`)
}
