package broker

import (
	"os"
	"path/filepath"
	"testing"
)

func writeLog(t *testing.T, dir, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, logFileName), []byte(content), 0o644); err != nil {
		t.Fatalf("write log: %v", err)
	}
}
