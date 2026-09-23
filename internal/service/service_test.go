package service_test

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"mirror-admission/internal/crypto/digest"
	"mirror-admission/internal/model"
	"mirror-admission/internal/policy"
	"mirror-admission/internal/service"
	"mirror-admission/internal/store"
	"mirror-admission/internal/testkit"
	"mirror-admission/internal/verifier"
)

const frozenPolicyPath = "../../policy/admission.rego"
const frozenLockPath = "../../policy/policy_freeze.lock.json"

func newEngine(t *testing.T) *policy.Engine {
	t.Helper()
	pol, hash, err := policy.LoadAndVerify(frozenPolicyPath, frozenLockPath)
	if err != nil {
		t.Fatal(err)
	}
	eng, err := policy.NewEngine(pol, hash)
	if err != nil {
		t.Fatal(err)
	}
	return eng
}

func newService(t *testing.T, now time.Time) (*service.Service, testkit.Keys) {
	t.Helper()
	k := testkit.GenerateKeys(t)
	svc := service.New(newEngine(t), k.Trusted(), testkit.Allowlist(), store.NewMemory()).
		WithClock(testkit.FixedClock{T: now})
	return svc, k
}

var now = time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC)

// findStatus returns a rule finding's status.
func findStatus(t *testing.T, r *model.Report, rule string) model.Finding {
	t.Helper()
	for _, f := range r.Findings {
		if f.Rule == rule {
			return f
		}
	}
	t.Fatalf("no finding for rule %s", rule)
	return model.Finding{}
}

func cleanMaterials(t *testing.T, k testkit.Keys, section map[string]any, base string) (model.AdmissionRequest, string) {
	t.Helper()
	cfgBytes := testkit.MustMarshal(t, testkit.Config(section))
	sbomBytes, _ := testkit.SBOM(t, cfgBytes, base)
	att := testkit.Attest(t, k, cfgBytes, sbomBytes, "pass")
	imageD, _, err := digest.ParseOCI(cfgBytes)
	if err != nil {
		t.Fatal(err)
	}
	return testkit.Request(cfgBytes, sbomBytes, att, nil, testkit.Allowlist().Version), imageD
}

func TestCleanImageAllows(t *testing.T) {
	svc, k := newService(t, now)
	req, _ := cleanMaterials(t, k,
		map[string]any{"User": "10001:10001", "Privileged": false}, testkit.AllowBase)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if r.Decision != "ALLOW" {
		t.Fatalf("want ALLOW, got %s: %+v", r.Decision, r.Findings)
	}
	if r.PolicyVersion != "1.4.0" || r.PolicySHA256 == "" {
		t.Fatalf("policy identity not frozen in report: %s %s", r.PolicyVersion, r.PolicySHA256)
	}
}

func TestRootVariantsDeny(t *testing.T) {
	svc, k := newService(t, now)
	for _, user := range []string{"root", "0", "0:0"} {
		req, _ := cleanMaterials(t, k,
			map[string]any{"User": user, "Privileged": false}, testkit.AllowBase)
		r, err := svc.Evaluate(context.Background(), req)
		if err != nil {
			t.Fatal(err)
		}
		if r.Decision != "DENY" {
			t.Fatalf("User=%s: want DENY got %s", user, r.Decision)
		}
		f := findStatus(t, r, "no_root_user")
		if f.Status != "deny" || !strings.Contains(f.Reason, "root") {
			t.Fatalf("User=%s bad finding: %+v", user, f)
		}
	}
}

func TestPrivilegedAndCapsDeny(t *testing.T) {
	svc, k := newService(t, now)
	cases := []map[string]any{
		{"User": "10001", "Privileged": true},
		{"User": "10001", "Privileged": false,
			"Capabilities": map[string]any{"Add": []string{"CAP_SYS_ADMIN"}}},
		{"User": "10001", "Privileged": false,
			"Capabilities": map[string]any{"Add": []string{"cap_sys_admin"}}}, // case-insensitive
	}
	for i, section := range cases {
		req, _ := cleanMaterials(t, k, section, testkit.AllowBase)
		r, err := svc.Evaluate(context.Background(), req)
		if err != nil {
			t.Fatal(err)
		}
		if findStatus(t, r, "no_privileged").Status != "deny" {
			t.Fatalf("case %d: want deny, got %+v", i, r.Findings)
		}
		if r.Decision != "DENY" {
			t.Fatalf("case %d want DENY got %s", i, r.Decision)
		}
	}
}

func TestBaseImageNotAllowedDenies(t *testing.T) {
	svc, k := newService(t, now)
	req, _ := cleanMaterials(t, k,
		map[string]any{"User": "10001", "Privileged": false}, testkit.DenyBase)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if r.Decision != "DENY" {
		t.Fatalf("want DENY got %s", r.Decision)
	}
	if !strings.Contains(findStatus(t, r, "base_image_allowed").Reason, "not on the allowlist") {
		t.Fatalf("bad reason: %+v", findStatus(t, r, "base_image_allowed"))
	}
}

// ---- missing evidence must be UNKNOWN, never default-allow ----------------

func TestMissingUserIsUnknownNotRootDeny(t *testing.T) {
	svc, k := newService(t, now)
	req, _ := cleanMaterials(t, k,
		map[string]any{"Privileged": false}, testkit.AllowBase)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	f := findStatus(t, r, "no_root_user")
	if f.Status != "unknown" {
		t.Fatalf("absent User must be unknown, got %s (%s)", f.Status, f.Reason)
	}
	if r.Decision != "UNKNOWN" {
		t.Fatalf("want UNKNOWN, got %s", r.Decision)
	}
}

func TestMissingPrivilegedIsUnknown(t *testing.T) {
	svc, k := newService(t, now)
	req, _ := cleanMaterials(t, k,
		map[string]any{"User": "10001"}, testkit.AllowBase)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if f := findStatus(t, r, "no_privileged"); f.Status != "unknown" {
		t.Fatalf("absent Privileged must be unknown, got %+v", f)
	}
}

func TestMissingAttestationIsUnknown(t *testing.T) {
	svc, _ := newService(t, now)
	cfgBytes := testkit.MustMarshal(t, testkit.Config(map[string]any{"User": "10001", "Privileged": false}))
	sbomBytes, _ := testkit.SBOM(t, cfgBytes, testkit.AllowBase)
	req := testkit.Request(cfgBytes, sbomBytes, nil, nil, testkit.Allowlist().Version)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if f := findStatus(t, r, "attestation_verified"); f.Status != "unknown" {
		t.Fatalf("missing attestation must be unknown, got %+v", f)
	}
	if r.Decision != "UNKNOWN" {
		t.Fatalf("missing attestation must gate admission as UNKNOWN, got %s", r.Decision)
	}
}

func TestBaseDigestMissingIsUnknown(t *testing.T) {
	svc, k := newService(t, now)
	cfgBytes := testkit.MustMarshal(t, testkit.Config(map[string]any{"User": "10001", "Privileged": false}))
	// SBOM without base_image_digest — attacker hopes the allowlist rule is skipped.
	imageD, _, _ := digest.ParseOCI(cfgBytes)
	sbomBytes := testkit.MustMarshal(t, map[string]any{"image_digest": imageD})
	att := testkit.Attest(t, k, cfgBytes, sbomBytes, "pass")
	req := testkit.Request(cfgBytes, sbomBytes, att, nil, testkit.Allowlist().Version)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if f := findStatus(t, r, "base_image_allowed"); f.Status != "unknown" {
		t.Fatalf("missing base digest must be unknown, got %+v", f)
	}
	if r.Decision != "UNKNOWN" {
		t.Fatalf("want UNKNOWN got %s", r.Decision)
	}
}

// ---- verifier result fail is authentic evidence of failure ----------------

func TestVerifierFailResultDenies(t *testing.T) {
	svc, k := newService(t, now)
	cfgBytes := testkit.MustMarshal(t, testkit.Config(map[string]any{"User": "10001", "Privileged": false}))
	sbomBytes, _ := testkit.SBOM(t, cfgBytes, testkit.AllowBase)
	att := testkit.Attest(t, k, cfgBytes, sbomBytes, "fail")
	req := testkit.Request(cfgBytes, sbomBytes, att, nil, testkit.Allowlist().Version)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if r.Decision != "DENY" || findStatus(t, r, "attestation_verified").Status != "deny" {
		t.Fatalf("verifier fail must deny: decision=%s", r.Decision)
	}
}

// ---- label drift: attestation bound to different config bytes --------------

func TestLabelDriftRebindsNothing(t *testing.T) {
	svc, k := newService(t, now)

	// Original image the tester attested.
	origBytes := testkit.MustMarshal(t, testkit.Config(map[string]any{"User": "10001", "Privileged": false}))
	sbomOrig, _ := testkit.SBOM(t, origBytes, testkit.AllowBase)
	att := testkit.Attest(t, k, origBytes, sbomOrig, "pass")

	// Attacker keeps the signed attestation but ships a config with an added
	// label and root user — the bytes (and canonical digest) differ.
	drifted := testkit.Config(map[string]any{"User": "root", "Privileged": false,
		"Labels": map[string]any{"registry.example/retag": "trusted:latest"}})
	driftBytes := testkit.MustMarshal(t, drifted)
	sbomDrift, _ := testkit.SBOM(t, driftBytes, testkit.AllowBase)

	// Re-presenting the old attestation against the drifted image must fail
	// the cryptographic binding: deny on attestation, deny on root. No
	// attestation is silently accepted as matching.
	req := testkit.Request(driftBytes, sbomDrift, att, nil, testkit.Allowlist().Version)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	attF := findStatus(t, r, "attestation_verified")
	if attF.Status != "deny" || !strings.Contains(attF.Reason, "evaluated image is") {
		t.Fatalf("label drift must fail digest binding: %+v", attF)
	}
	if r.Decision != "DENY" {
		t.Fatalf("want DENY got %s", r.Decision)
	}
}

// ---- exemptions: scope, rule, expiry boundary -----------------------------

func TestValidExemptionDowngradesOnlyItsRule(t *testing.T) {
	svc, k := newService(t, now)
	cfgBytes := testkit.MustMarshal(t, testkit.Config(map[string]any{"User": "root", "Privileged": false}))
	sbomBytes, _ := testkit.SBOM(t, cfgBytes, testkit.AllowBase)
	att := testkit.Attest(t, k, cfgBytes, sbomBytes, "pass")
	imageD, _, _ := digest.ParseOCI(cfgBytes)
	ex := testkit.Exempt(t, k, "ex-1", imageD, "no_root_user",
		now.Add(24*time.Hour).Format(time.RFC3339))
	req := testkit.Request(cfgBytes, sbomBytes, att, [][]byte{ex}, testkit.Allowlist().Version)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if r.Decision != "ALLOW" {
		t.Fatalf("valid exemption should yield ALLOW, got %s: %+v", r.Decision, r.Findings)
	}
	f := findStatus(t, r, "no_root_user")
	if f.Status != "exempt" || len(f.Exemptions) != 1 || f.Exemptions[0] != "ex-1" {
		t.Fatalf("rule should be exempt by ex-1: %+v", f)
	}
}

func TestExemptionBoundaryNotAfterEqualsNowIsExpired(t *testing.T) {
	// Engine time pinned exactly to the expiry instant.
	svc, k := newService(t, now)
	cfgBytes := testkit.MustMarshal(t, testkit.Config(map[string]any{"User": "root", "Privileged": false}))
	sbomBytes, _ := testkit.SBOM(t, cfgBytes, testkit.AllowBase)
	att := testkit.Attest(t, k, cfgBytes, sbomBytes, "pass")
	imageD, _, _ := digest.ParseOCI(cfgBytes)
	ex := testkit.Exempt(t, k, "ex-boundary", imageD, "no_root_user", now.Format(time.RFC3339))
	req := testkit.Request(cfgBytes, sbomBytes, att, [][]byte{ex}, testkit.Allowlist().Version)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if r.Decision != "DENY" {
		t.Fatalf("not_after==now must be expired (deny stands), got %s", r.Decision)
	}
	if findStatus(t, r, "no_root_user").Status != "deny" {
		t.Fatal("root finding must remain deny at boundary")
	}
	if len(r.UnusedExemptions) != 1 || !strings.Contains(r.UnusedExemptions[0].Reason, "expired") {
		t.Fatalf("exemption must be reported expired: %+v", r.UnusedExemptions)
	}
}

func TestExemptionOneSecondBeforeIsLive(t *testing.T) {
	expiry := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	svc, k := newService(t, expiry.Add(-time.Second))
	cfgBytes := testkit.MustMarshal(t, testkit.Config(map[string]any{"User": "root", "Privileged": false}))
	sbomBytes, _ := testkit.SBOM(t, cfgBytes, testkit.AllowBase)
	att := testkit.Attest(t, k, cfgBytes, sbomBytes, "pass")
	imageD, _, _ := digest.ParseOCI(cfgBytes)
	ex := testkit.Exempt(t, k, "ex-near", imageD, "no_root_user", expiry.Format(time.RFC3339))
	req := testkit.Request(cfgBytes, sbomBytes, att, [][]byte{ex}, testkit.Allowlist().Version)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if r.Decision != "ALLOW" {
		t.Fatalf("one second before expiry exemption must be live, got %s", r.Decision)
	}
}

func TestExemptionWrongDigestDoesNotApply(t *testing.T) {
	svc, k := newService(t, now)
	cfgBytes := testkit.MustMarshal(t, testkit.Config(map[string]any{"User": "root", "Privileged": false}))
	sbomBytes, _ := testkit.SBOM(t, cfgBytes, testkit.AllowBase)
	att := testkit.Attest(t, k, cfgBytes, sbomBytes, "pass")
	ex := testkit.Exempt(t, k, "ex-other", "sha256:"+strings.Repeat("c", 64),
		"no_root_user", now.Add(time.Hour).Format(time.RFC3339))
	req := testkit.Request(cfgBytes, sbomBytes, att, [][]byte{ex}, testkit.Allowlist().Version)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if r.Decision != "DENY" {
		t.Fatal("exemption for a different digest must not waive")
	}
	if findStatus(t, r, "no_root_user").Status != "deny" {
		t.Fatal("root deny must stand")
	}
	if len(r.UnusedExemptions) != 1 || !strings.Contains(r.UnusedExemptions[0].Reason, "other digest") {
		t.Fatalf("wrong-digest exemption must be explained: %+v", r.UnusedExemptions)
	}
}

func TestExemptionNeverWaivesUnknown(t *testing.T) {
	svc, k := newService(t, now)
	// Image missing User: root rule is UNKNOWN. An exemption naming that rule
	// must NOT turn unknown into allow — missing evidence is not waivable.
	cfgBytes := testkit.MustMarshal(t, testkit.Config(map[string]any{"Privileged": false}))
	sbomBytes, _ := testkit.SBOM(t, cfgBytes, testkit.AllowBase)
	att := testkit.Attest(t, k, cfgBytes, sbomBytes, "pass")
	imageD, _, _ := digest.ParseOCI(cfgBytes)
	ex := testkit.Exempt(t, k, "ex-unk", imageD, "no_root_user",
		now.Add(time.Hour).Format(time.RFC3339))
	req := testkit.Request(cfgBytes, sbomBytes, att, [][]byte{ex}, testkit.Allowlist().Version)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if findStatus(t, r, "no_root_user").Status != "unknown" {
		t.Fatalf("exemption must not downgrade unknown: %+v", findStatus(t, r, "no_root_user"))
	}
	if r.Decision != "UNKNOWN" {
		t.Fatalf("decision must remain UNKNOWN, got %s", r.Decision)
	}
}

// ---- forged / cross-signed evidence is rejected ---------------------------

func TestExemptionSignedByTesterKeyRejected(t *testing.T) {
	svc, k := newService(t, now)
	cfgBytes := testkit.MustMarshal(t, testkit.Config(map[string]any{"User": "root", "Privileged": false}))
	sbomBytes, _ := testkit.SBOM(t, cfgBytes, testkit.AllowBase)
	att := testkit.Attest(t, k, cfgBytes, sbomBytes, "pass")
	imageD, _, _ := digest.ParseOCI(cfgBytes)

	// Forge an exemption envelope using the TESTER key with exemption payload
	// type. Separation of trust roots: signature must fail.
	payload := model.ExemptionPayload{
		Kind: verifier.ExemptionKind, ID: "forged", SignerKeyID: k.VerifierID,
		ImageDigest: imageD, Rule: "no_root_user",
		NotAfter: now.Add(time.Hour).Format(time.RFC3339),
	}
	forged, err := verifier.SealExemption(k.VerifierPriv, payload)
	if err != nil {
		t.Fatal(err)
	}
	req := testkit.Request(cfgBytes, sbomBytes, att, [][]byte{forged}, testkit.Allowlist().Version)
	if _, err := svc.Evaluate(context.Background(), req); err == nil {
		t.Fatal("tester-signed exemption must be rejected")
	} else if !errors.Is(err, service.ErrBadRequest) {
		t.Fatalf("want bad request, got %v", err)
	}
}

func TestTamperedAttestationRejected(t *testing.T) {
	svc, k := newService(t, now)
	cfgBytes := testkit.MustMarshal(t, testkit.Config(map[string]any{"User": "10001", "Privileged": false}))
	sbomBytes, _ := testkit.SBOM(t, cfgBytes, testkit.AllowBase)
	att := testkit.Attest(t, k, cfgBytes, sbomBytes, "pass")

	// Flip a byte of the signature field inside the envelope JSON.
	var env map[string]any
	if err := json.Unmarshal(att, &env); err != nil {
		t.Fatal(err)
	}
	s0 := env["signature"].(string)
	b := []byte(s0)
	if b[10] == 'A' {
		b[10] = 'B'
	} else {
		b[10] = 'A'
	}
	env["signature"] = string(b)
	tampered, _ := json.Marshal(env)

	req := testkit.Request(cfgBytes, sbomBytes, tampered, nil, testkit.Allowlist().Version)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if findStatus(t, r, "attestation_verified").Status != "deny" {
		t.Fatal("tampered signature must make attestation deny")
	}
	if r.Decision != "DENY" {
		t.Fatalf("want DENY got %s", r.Decision)
	}
}

func TestAllowlistVersionMismatchRejected(t *testing.T) {
	svc, k := newService(t, now)
	req, _ := cleanMaterials(t, k,
		map[string]any{"User": "10001", "Privileged": false}, testkit.AllowBase)
	req.AllowlistVersion = "allowlist-1999.01.01"
	if _, err := svc.Evaluate(context.Background(), req); !errors.Is(err, service.ErrBadRequest) {
		t.Fatalf("stale allowlist version must be rejected, got %v", err)
	}
}

func TestMalformedJSONRejected(t *testing.T) {
	svc, _ := newService(t, now)
	for _, req := range []model.AdmissionRequest{
		{SBOM: []byte("{}"), AllowlistVersion: "x"},
		{ImageConfig: []byte("{not json"), SBOM: []byte("{}"), AllowlistVersion: "x"},
		{ImageConfig: []byte(`{"config":{}}`)}, // missing sbom/version
	} {
		if _, err := svc.Evaluate(context.Background(), req); !errors.Is(err, service.ErrBadRequest) {
			t.Fatalf("malformed request must be bad request: %+v err=%v", req, err)
		}
	}
}

// ---- reports are immutable and never overwritten on re-evaluation ---------

func TestReevaluationAppendsNewReportNeverOverwrites(t *testing.T) {
	svc, k := newService(t, now)
	req, _ := cleanMaterials(t, k,
		map[string]any{"User": "10001", "Privileged": false}, testkit.AllowBase)

	r1, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	r2, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if r1.ID == r2.ID {
		t.Fatal("re-evaluation reused report id (overwrite risk)")
	}
	if r1.ImageDigest != r2.ImageDigest {
		t.Fatal("same input produced different digest")
	}
	got1, err := svc.GetReport(context.Background(), r1.ID)
	if err != nil || got1.Decision != r1.Decision {
		t.Fatalf("original report mutated/lost: %v %v", got1, err)
	}
	list, err := svc.ListReports(context.Background())
	if err != nil || len(list) != 2 {
		t.Fatalf("want 2 append-only reports, got %d (err=%v)", len(list), err)
	}
	if _, err := svc.GetReport(context.Background(), "rpt_doesnotexist"); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("unknown id must be ErrNotFound, got %v", err)
	}
}

func TestEveryRuleProducesReason(t *testing.T) {
	svc, k := newService(t, now)
	req, _ := cleanMaterials(t, k,
		map[string]any{"User": "10001", "Privileged": false}, testkit.AllowBase)
	r, err := svc.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]bool{
		"no_root_user": false, "no_privileged": false,
		"base_image_allowed": false, "attestation_verified": false,
	}
	for _, f := range r.Findings {
		if f.Reason == "" {
			t.Fatalf("rule %s returned empty reason", f.Rule)
		}
		want[f.Rule] = true
	}
	for rule, seen := range want {
		if !seen {
			t.Fatalf("no finding emitted for %s", rule)
		}
	}
}
