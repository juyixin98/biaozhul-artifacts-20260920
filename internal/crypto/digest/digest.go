package digest

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"

	"mirror-admission/internal/crypto/sig"
)

// SHA256 returns "sha256:<hex>" over the given bytes.
func SHA256(b []byte) string {
	sum := sha256.Sum256(b)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// CanonicalSHA256 returns the digest over the canonical-JSON form of the
// document. Evidence is bound to this digest so signatures survive benign
// re-indentation on the wire while still breaking on any content change
// (including a re-tag: the tag is never part of the identity, the config is).
func CanonicalSHA256(raw []byte) (string, []byte, error) {
	canon, err := sig.CanonicalJSON(raw)
	if err != nil {
		return "", nil, err
	}
	return SHA256(canon), canon, nil
}

// ParseOCI validates the raw image config and returns its canonical digest
// plus the parsed generic object.
func ParseOCI(rawConfig []byte) (string, map[string]any, error) {
	if len(rawConfig) == 0 {
		return "", nil, fmt.Errorf("image_config is empty")
	}
	d, _, err := CanonicalSHA256(rawConfig)
	if err != nil {
		return "", nil, fmt.Errorf("image_config: %w", err)
	}
	var cfg map[string]any
	if err := json.Unmarshal(rawConfig, &cfg); err != nil {
		return "", nil, fmt.Errorf("image_config is not valid JSON: %w", err)
	}
	if _, ok := cfg["config"]; !ok {
		return "", nil, fmt.Errorf("image_config lacks the OCI \"config\" section")
	}
	return d, cfg, nil
}

// ParseSBOM validates the raw SBOM and returns its canonical digest.
func ParseSBOM(rawSBOM []byte) (string, map[string]any, error) {
	if len(rawSBOM) == 0 {
		return "", nil, fmt.Errorf("sbom is empty")
	}
	d, _, err := CanonicalSHA256(rawSBOM)
	if err != nil {
		return "", nil, fmt.Errorf("sbom: %w", err)
	}
	var s map[string]any
	if err := json.Unmarshal(rawSBOM, &s); err != nil {
		return "", nil, fmt.Errorf("sbom is not valid JSON: %w", err)
	}
	return d, s, nil
}
