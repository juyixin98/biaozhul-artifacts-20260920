package provenance

import (
	"bytes"
	"encoding/hex"
	"path/filepath"
)

func filepathDir(p string) string { return filepath.Dir(p) }

func bytesReader(b []byte) *bytes.Reader { return bytes.NewReader(b) }

// bytesHex decodes s, returning nil on invalid input; hmac.Equal on nil
// slices simply reports inequality, which is the desired behavior.
func bytesHex(s string) []byte {
	b, err := hex.DecodeString(s)
	if err != nil {
		return nil
	}
	return b
}
