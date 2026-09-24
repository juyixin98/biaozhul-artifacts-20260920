// Package crypto performs the gateway's real cryptographic operations:
// canonical HMAC-SHA256 signing and verification of telemetry records.
package cryptox

import (
	"crypto/hmac"
	"crypto/sha256"
	"sort"
	"strconv"
	"strings"
)

// CanonicalV1 builds the deterministic byte string that is HMACed for a v1
// record. The signature field itself is never part of the input. Labels are
// sorted by key so identical records always produce identical bytes. The
// optional battery is encoded distinctly as "-" (absent) vs its number so an
// explicit zero and an unset field have different signatures.
//
// Format (pipe-delimited, newlines forbidden inside values by construction):
//
//	v1|device_id|timestamp_ms|temp_milli_c|status|k=v,k=v|request_id|battery
func CanonicalV1(deviceID string, timestampMs, tempMilliC int64, status int32,
	labels map[string]string, requestID string, battery *int64) []byte {
	var b strings.Builder
	b.WriteString("v1|")
	b.WriteString(escape(deviceID))
	b.WriteByte('|')
	b.WriteString(strconv.FormatInt(timestampMs, 10))
	b.WriteByte('|')
	b.WriteString(strconv.FormatInt(tempMilliC, 10))
	b.WriteByte('|')
	b.WriteString(strconv.FormatInt(int64(status), 10))
	b.WriteByte('|')
	writeLabels(&b, labels)
	b.WriteByte('|')
	b.WriteString(escape(requestID))
	b.WriteByte('|')
	writeOptionalInt64(&b, battery)
	return []byte(b.String())
}

// CanonicalV2 is the v2 counterpart.
//
//	v2|device_id|observed_at_ns|temperature_uk|phase|k=v,k=v|trace_id|battery
func CanonicalV2(deviceID string, observedAtNs, temperatureUK int64, phase int32,
	labels map[string]string, traceID string, battery *int64) []byte {
	var b strings.Builder
	b.WriteString("v2|")
	b.WriteString(escape(deviceID))
	b.WriteByte('|')
	b.WriteString(strconv.FormatInt(observedAtNs, 10))
	b.WriteByte('|')
	b.WriteString(strconv.FormatInt(temperatureUK, 10))
	b.WriteByte('|')
	b.WriteString(strconv.FormatInt(int64(phase), 10))
	b.WriteByte('|')
	writeLabels(&b, labels)
	b.WriteByte('|')
	b.WriteString(escape(traceID))
	b.WriteByte('|')
	writeOptionalInt64(&b, battery)
	return []byte(b.String())
}

func writeOptionalInt64(b *strings.Builder, v *int64) {
	if v == nil {
		b.WriteByte('-')
		return
	}
	b.WriteString(strconv.FormatInt(*v, 10))
}

func writeLabels(b *strings.Builder, labels map[string]string) {
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for i, k := range keys {
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteString(escape(k))
		b.WriteByte('=')
		b.WriteString(escape(labels[k]))
	}
}

// escape makes the encoding injective with respect to the delimiters.
func escape(s string) string {
	s = strings.ReplaceAll(s, `\`, `\\`)
	s = strings.ReplaceAll(s, "|", `\p`)
	s = strings.ReplaceAll(s, ",", `\c`)
	s = strings.ReplaceAll(s, "=", `\e`)
	s = strings.ReplaceAll(s, "\n", `\n`)
	return s
}

// Sign returns HMAC-SHA256(key, canonical).
func Sign(key string, canonical []byte) []byte {
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write(canonical)
	return mac.Sum(nil)
}

// Verify compares in constant time.
func Verify(key string, canonical, signature []byte) bool {
	want := Sign(key, canonical)
	return hmac.Equal(want, signature)
}
