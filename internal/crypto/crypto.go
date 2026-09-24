// Package crypto implements the real cryptographic operations of the service:
//   - ConfigVersion: SHA-256 over the canonical JSON of a scaling config, so
//     identical configurations always map to the same version.
//   - SignDecision / VerifySignature: HMAC-SHA256 over a canonical decision
//     string, hex-encoded.
//
// Nothing here is stubbed; the standard library implementations are used.
package crypto

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// CanonicalConfig serializes the config through encoding/json so all numeric
// fields are emitted deterministically (Go sorts map keys; the struct has a
// fixed field order, both give a stable byte representation).
func CanonicalConfig(min, max int, target, tolerance float64, stableWindowSec int) ([]byte, error) {
	payload := struct {
		MinReplicas     int     `json:"minReplicas"`
		MaxReplicas     int     `json:"maxReplicas"`
		TargetPct       float64 `json:"targetPct"`
		TolerancePct    float64 `json:"tolerancePct"`
		StableWindowSec int     `json:"stableWindowSec"`
	}{min, max, target, tolerance, stableWindowSec}
	b, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("canonical config: %w", err)
	}
	return b, nil
}

// ConfigVersion returns the hex SHA-256 digest of the canonical config,
// truncated to 16 hex chars (64 bits) prefixed with "cfg_" — enough entropy
// to distinguish versions while staying readable.
func ConfigVersion(min, max int, target, tolerance float64, stableWindowSec int) (string, error) {
	b, err := CanonicalConfig(min, max, target, tolerance, stableWindowSec)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return "cfg_" + hex.EncodeToString(sum[:])[:16], nil
}

// canonicalDecision builds the exact byte string that is signed. Fields are
// fixed-width in a fixed order with a '|' delimiter; floats use 'g' formatting.
func canonicalDecision(workload, configVersion, action, reasons string,
	metricTimeMs, currentReplicas, rawProposed, stabilized, finalReplicas int64,
	avgUtilizationPct, targetPct, ratio float64) string {
	return strings.Join([]string{
		"v1",
		"scaling-decision",
		workload,
		configVersion,
		strconv.FormatInt(metricTimeMs, 10),
		strconv.FormatInt(currentReplicas, 10),
		strconv.FormatFloat(avgUtilizationPct, 'g', -1, 64),
		strconv.FormatFloat(targetPct, 'g', -1, 64),
		strconv.FormatFloat(ratio, 'g', -1, 64),
		strconv.FormatInt(rawProposed, 10),
		strconv.FormatInt(stabilized, 10),
		strconv.FormatInt(finalReplicas, 10),
		action,
		reasons,
	}, "|")
}

// SignDecisionParameters carries the decision fields covered by the signature.
type SignDecisionParameters struct {
	Workload          string
	ConfigVersion     string
	MetricTimeMs      int64
	CurrentReplicas   int
	AvgUtilizationPct float64
	TargetPct         float64
	Ratio             float64
	RawProposed       int
	Stabilized        int
	FinalReplicas     int
	Action            string
	Reasons           string // comma-separated, as persisted
}

// SignDecision returns the hex HMAC-SHA256 of the canonical decision string.
func SignDecision(secret []byte, p SignDecisionParameters) string {
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(canonicalDecision(
		p.Workload, p.ConfigVersion, p.Action, p.Reasons,
		p.MetricTimeMs, int64(p.CurrentReplicas), int64(p.RawProposed),
		int64(p.Stabilized), int64(p.FinalReplicas),
		p.AvgUtilizationPct, p.TargetPct, p.Ratio)))
	return hex.EncodeToString(mac.Sum(nil))
}

// VerifySignature performs a constant-time comparison of a provided hex
// signature against one recomputed from the decision fields.
func VerifySignature(secret []byte, p SignDecisionParameters, providedHex string) bool {
	expected := SignDecision(secret, p)
	provided, err := hex.DecodeString(providedHex)
	if err != nil {
		return false
	}
	expectedBytes, _ := hex.DecodeString(expected)
	return hmac.Equal(provided, expectedBytes)
}
