package worker

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"image/png"
	"io"
	"os"
	"path/filepath"
)

func readAll(f *os.File) ([]byte, error) {
	var buf bytes.Buffer
	if _, err := io.Copy(&buf, f); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func sha256hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func filepathDir(p string) string { return filepath.Dir(p) }

// pngDimensions decodes just enough of the PNG to obtain its size and also
// fully validates that the bytes are a readable PNG (the later render
// decodes pixels).
func pngDimensions(data []byte) (struct{ w, h int }, error) {
	cfg, err := png.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		return struct{ w, h int }{}, err
	}
	return struct{ w, h int }{cfg.Width, cfg.Height}, nil
}
