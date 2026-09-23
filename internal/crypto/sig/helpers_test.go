package sig_test

import (
	"encoding/base64"
	"testing"
)

func decodeB64(t *testing.T, s string) []byte {
	t.Helper()
	b, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func encodeB64(b []byte) string { return base64.StdEncoding.EncodeToString(b) }
