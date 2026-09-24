package builder

import (
	"os/exec"
	"strings"
	"testing"
)

// runBlobAt executes an artifact blob by absolute path.
func runBlobAt(t *testing.T, abs string) string {
	t.Helper()
	out, err := exec.Command(abs).Output()
	if err != nil {
		t.Fatalf("run artifact %s: %v", abs, err)
	}
	return strings.TrimSpace(string(out))
}
