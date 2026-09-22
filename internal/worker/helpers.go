package worker

import (
	"bytes"
	"encoding/json"
	"os"
)

func jsonUnmarshal(data []byte, v any) error { return json.Unmarshal(data, v) }

func byteReader(b []byte) *bytes.Reader { return bytes.NewReader(b) }

// renameOver atomically replaces dst with src. Both are in the same directory
// (caller guarantees), so os.Rename is atomic on POSIX; a pre-existing dst is
// replaced.
func renameOver(src, dst string) error { return os.Rename(src, dst) }
