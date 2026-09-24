package crypto

import "strconv"

// formatFloat is the canonical, round-trip-stable rendering of a JSON number
// parsed into float64. Both signer and verifier parse the JSON number as
// float64 and re-format it the same way, so equal float64 values always yield
// the same canonical string.
func formatFloat(v float64) string {
	return strconv.FormatFloat(v, 'g', -1, 64)
}
