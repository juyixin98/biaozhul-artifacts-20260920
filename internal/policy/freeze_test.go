package policy_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"mirror-admission/internal/policy"
)

const policyPath = "../../policy/admission.rego"
const lockPath = "../../policy/policy_freeze.lock.json"

func TestFrozenPolicyLoadsAndSelfChecksVersion(t *testing.T) {
	pol, hash, err := policy.LoadAndVerify(policyPath, lockPath)
	if err != nil {
		t.Fatal(err)
	}
	if hash == "" || len(hash) != 64 {
		t.Fatalf("bad policy hash %q", hash)
	}
	eng, err := policy.NewEngine(pol, hash)
	if err != nil {
		t.Fatal(err)
	}
	if eng.Version() != "1.4.0" {
		t.Fatalf("version drift: %s", eng.Version())
	}
	if eng.SHA256() != hash {
		t.Fatal("engine hash not frozen from lock")
	}
}

func TestTamperedPolicyFailsFreezeLock(t *testing.T) {
	dir := t.TempDir()
	pol := filepath.Join(dir, "admission.rego")
	lock := filepath.Join(dir, "lock.json")

	original, err := os.ReadFile(policyPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(pol, original, 0o600); err != nil {
		t.Fatal(err)
	}
	lockBytes, err := os.ReadFile(lockPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(lock, lockBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := policy.LoadAndVerify(pol, lock); err != nil {
		t.Fatalf("copied policy should verify: %v", err)
	}

	// Tamper a single byte inside the policy text and expect refusal.
	tampered := append([]byte(nil), original...)
	idx := strings.Index(string(tampered), "1.4.0")
	if idx < 0 {
		t.Fatal("version marker not found")
	}
	tampered[idx] = '9'
	if err := os.WriteFile(pol, tampered, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := policy.LoadAndVerify(pol, lock); err == nil ||
		!strings.Contains(err.Error(), "FREEZE") {
		t.Fatalf("tampered policy must trip the freeze violation, got %v", err)
	}
}
