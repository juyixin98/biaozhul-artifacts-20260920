// Package policy compiles and evaluates the frozen Rego admission policy.
package policy

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
)

// FreezeLock is checked byte-for-hash at startup. Regenerating it is an
// explicit, noisy act (`make policy-lock`); a running server will not start
// against a silently modified policy.
type FreezeLock struct {
	PolicyVersion string `json:"policy_version"`
	PolicyPath    string `json:"policy_path"`
	SHA256        string `json:"sha256"`
	RegoPackage   string `json:"rego_package"`
}

// PolicyVersion is the single frozen version string. It must match the
// version declared inside admission.rego.
const PolicyVersion = "1.4.0"

// ComputeSHA256 hashes the policy bytes.
func ComputeSHA256(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// LoadAndVerify reads the policy and its freeze lock, refusing any mismatch.
func LoadAndVerify(policyPath, lockPath string) ([]byte, string, error) {
	pol, err := os.ReadFile(policyPath)
	if err != nil {
		return nil, "", fmt.Errorf("read policy: %w", err)
	}
	lockBytes, err := os.ReadFile(lockPath)
	if err != nil {
		return nil, "", fmt.Errorf("read policy freeze lock: %w", err)
	}
	var lock FreezeLock
	if err := json.Unmarshal(lockBytes, &lock); err != nil {
		return nil, "", fmt.Errorf("parse policy freeze lock: %w", err)
	}
	got := ComputeSHA256(pol)
	if got != lock.SHA256 {
		return nil, "", fmt.Errorf("POLICY FREEZE VIOLATION: %s hash %s does not match frozen lock %s — regenerate policy_freeze.lock only via `make policy-lock`", policyPath, got, lock.SHA256)
	}
	if lock.PolicyVersion != PolicyVersion {
		return nil, "", fmt.Errorf("POLICY FREEZE VIOLATION: lock version %q != engine version %q", lock.PolicyVersion, PolicyVersion)
	}
	return pol, got, nil
}
