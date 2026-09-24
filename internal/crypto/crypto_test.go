package crypto

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"testing"
)

func TestConfigVersionDeterministicAndSensitive(t *testing.T) {
	v1, err := ConfigVersion(1, 10, 50, 10, 60)
	if err != nil {
		t.Fatal(err)
	}
	v2, err := ConfigVersion(1, 10, 50, 10, 60)
	if err != nil {
		t.Fatal(err)
	}
	if v1 != v2 || v1 == "" || v1[:4] != "cfg_" {
		t.Fatalf("versions must be deterministic and prefixed: %q %q", v1, v2)
	}
	// Any parameter change must change the version.
	v3, _ := ConfigVersion(1, 10, 60, 10, 60) // target changed
	v4, _ := ConfigVersion(1, 9, 50, 10, 60)  // max changed
	v5, _ := ConfigVersion(1, 10, 50, 10, 30) // window changed
	for _, v := range []string{v3, v4, v5} {
		if v == v1 {
			t.Fatalf("config change did not change version: %q", v)
		}
	}

	// Verify against a raw SHA-256 of the canonical JSON independently.
	b, err := CanonicalConfig(1, 10, 50, 10, 60)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(b)
	if want := "cfg_" + hex.EncodeToString(sum[:])[:16]; want != v1 {
		t.Fatalf("version %q != independently computed %q", v1, want)
	}
}

func TestSignAndVerifyRoundTrip(t *testing.T) {
	secret := []byte("test-secret")
	p := SignDecisionParameters{
		Workload: "web", ConfigVersion: "cfg_abc", MetricTimeMs: 1700_000_000_000,
		CurrentReplicas: 4, AvgUtilizationPct: 90, TargetPct: 50, Ratio: 1.8,
		RawProposed: 8, Stabilized: 8, FinalReplicas: 8,
		Action: "scaleup", Reasons: "RAW_ROUNDED_UP,SCALE_UP_IMMEDIATE",
	}
	sig := SignDecision(secret, p)
	if len(sig) != 64 { // hex SHA-256
		t.Fatalf("signature length %d", len(sig))
	}
	if !VerifySignature(secret, p, sig) {
		t.Fatal("valid signature failed verification")
	}
	// Tamper with one field -> verification fails.
	tampered := p
	tampered.FinalReplicas = 9
	if VerifySignature(secret, tampered, sig) {
		t.Fatal("tampered decision verified")
	}
	// Wrong secret -> fails.
	if VerifySignature([]byte("other"), p, sig) {
		t.Fatal("signature verified with wrong secret")
	}
	// Garbage hex -> fails rather than panicking.
	if VerifySignature(secret, p, "not-hex") {
		t.Fatal("garbage signature verified")
	}
}

func TestSignatureMatchesManualHMAC(t *testing.T) {
	// Independent recomputation guards against the canonical format drifting.
	secret := []byte("k")
	p := SignDecisionParameters{
		Workload: "w", ConfigVersion: "cfg_v", MetricTimeMs: 1000,
		CurrentReplicas: 3, AvgUtilizationPct: 80, TargetPct: 50, Ratio: 1.6,
		RawProposed: 5, Stabilized: 5, FinalReplicas: 5,
		Action: "scaleup", Reasons: "R1,R2",
	}
	canonical := "v1|scaling-decision|w|cfg_v|1000|3|80|50|1.6|5|5|5|scaleup|R1,R2"
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(canonical))
	want := hex.EncodeToString(mac.Sum(nil))
	if got := SignDecision(secret, p); got != want {
		t.Fatalf("signature mismatch\ngot  %s\nwant %s", got, want)
	}
}
