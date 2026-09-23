package builder

import (
	"os"
	"path/filepath"
)

func writeFile(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o644)
}

func removeFile(path string) error {
	return os.Remove(path)
}
