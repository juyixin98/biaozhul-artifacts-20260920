package attestation_test

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"strings"
	"sync"
	"testing"
	"time"

	"build-attestation/internal/attestation"
	"build-attestation/internal/canonical"
	"build-attestation/internal/testkeys"
)

const (
	digestOK  = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	repoOK    = "https://github.com/example-org/app"
	builderOK = "ci.example.net"
)

type clock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *clock) get() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}
func (c *clock) set(t time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = t
}

func testPolicy(t *testing.T) *attestation.Policy {
	t.Helper()
	cur, old, rev := testkeys.CI()
	other := testkeys.Other()
	t1 := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	t2 := time.Date(2027, 1, 1, 0, 0, 0, 0, time.UTC)
	revokedAt := time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)

	doc := map[string]any{
		"allowedSourcePrefixes": []string{"https://github.com/example-org/"},
		"builders": []map[string]any{
			{"id": builderOK, "keys": []map[string]any{
				{"id": cur.ID, "publicKey": cur.PublicHex, "notBefore": t1, "notAfter": t2},
				{"id": old.ID, "publicKey": old.PublicHex, "notBefore": time.Date(2025, 1, 1, 0, 0, 0, 0, time.UTC), "notAfter": t1},
				{"id": rev.ID, "publicKey": rev.PublicHex, "notBefore": t1, "notAfter": t2, "revokedAt": revokedAt},
			}},
			{"id": "other-builder.example.com", "keys": []map[string]any{
				{"id": other.ID, "publicKey": other.PublicHex, "notBefore": t1, "notAfter": t2},
			}},
		},
	}
	raw, _ := json.Marshal(doc)
	p, err := attestation.ParsePolicy(raw)
	if err != nil {
		t.Fatalf("policy: %v", err)
	}
	return p
}

func newHarness(t *testing.T) (*attestation.Verifier, *clock) {
	t.Helper()
	clk := &clock{t: time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)}
	v, err := attestation.NewVerifier(attestation.Config{
		Policy: testPolicy(t),
		Now:    clk.get,
	})
	if err != nil {
		t.Fatal(err)
	}
	return v, clk
}

func statement(now time.Time, keyID string) *attestation.Statement {
	return &attestation.Statement{
		Type:      attestation.StatementType,
		BuilderID: builderOK,
		KeyID:     keyID,
		Source:    attestation.Source{Repository: repoOK, Commit: "sha1:" + strings.Repeat("a", 40)},
		Subjects:  []attestation.Digest{{Alg: "sha256", Value: digestOK}},
		Params:    map[string]any{"go": "1.22", "reproducible": true, "jobs": 4},
		IssuedAt:  now.Format(time.RFC3339),
		Nonce:     "nonce-aaaaaaaaaaaaaaaa",
	}
}

func marshalEnvelope(t *testing.T, env *attestation.Envelope) []byte {
	t.Helper()
	raw, err := json.Marshal(env)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func sign(t *testing.T, st *attestation.Statement, key ed25519.PrivateKey) []byte {
	t.Helper()
	env, err := attestation.Sign(st, key)
	if err != nil {
		t.Fatal(err)
	}
	return marshalEnvelope(t, env)
}

func expectCode(t *testing.T, v *attestation.Verifier, raw []byte, wantCode string) {
	t.Helper()
	res, err := v.Verify(raw)
	if wantCode == "" {
		if err != nil {
			t.Fatalf("expected acceptance, got %s: %s", err.Code, err.Message)
		}
		if res.BuilderID != builderOK {
			t.Fatalf("unexpected result: %+v", res)
		}
		return
	}
	if err == nil {
		t.Fatalf("expected %s, got acceptance (%+v)", wantCode, res)
	}
	if err.Code != wantCode {
		t.Fatalf("expected %s, got %s: %s", wantCode, err.Code, err.Message)
	}
}

func TestValidAcceptance(t *testing.T) {
	v, clk := newHarness(t)
	cur, _, _ := testkeys.CI()
	raw := sign(t, statement(clk.get(), cur.ID), cur.Private)
	expectCode(t, v, raw, "")
}

// The signature binds the artifact digest: changing it after signing fails
// Ed25519 verification.
func TestTamperedDigest(t *testing.T) {
	v, clk := newHarness(t)
	cur, _, _ := testkeys.CI()
	env, err := attestation.Sign(statement(clk.get(), cur.ID), cur.Private)
	if err != nil {
		t.Fatal(err)
	}
	goodSig := env.Signature

	tampered := statement(clk.get(), cur.ID)
	tampered.Subjects[0].Value = strings.Repeat("f", 64)
	env2, err := attestation.Sign(tampered, cur.Private)
	if err != nil {
		t.Fatal(err)
	}
	env2.Signature = goodSig // signature belongs to the old digest
	expectCode(t, v, marshalEnvelope(t, env2), attestation.CodeBadSignature)
}

// Tampering with source commit or params must also break the signature.
func TestTamperedCommitAndParams(t *testing.T) {
	v, clk := newHarness(t)
	cur, _, _ := testkeys.CI()
	env, err := attestation.Sign(statement(clk.get(), cur.ID), cur.Private)
	if err != nil {
		t.Fatal(err)
	}
	st := statement(clk.get(), cur.ID)
	st.Source.Commit = "sha1:" + strings.Repeat("b", 40)
	env2, _ := attestation.Sign(st, cur.Private)
	env2.Signature = env.Signature
	expectCode(t, v, marshalEnvelope(t, env2), attestation.CodeBadSignature)

	st3 := statement(clk.get(), cur.ID)
	st3.Params["jobs"] = json.Number("16")
	env3, _ := attestation.Sign(st3, cur.Private)
	env3.Signature = env.Signature
	expectCode(t, v, marshalEnvelope(t, env3), attestation.CodeBadSignature)
}

// Old key: rotated away before the attestation was issued.
func TestOldKeyRejected(t *testing.T) {
	v, clk := newHarness(t)
	_, old, _ := testkeys.CI()
	st := statement(clk.get(), old.ID)
	expectCode(t, v, sign(t, st, old.Private), attestation.CodeKeyExpired)
}

// A revoked key is rejected for timestamps at/after revocation but accepted
// before it — verifying rotation windows are evaluated at issuance time.
func TestRevokedKeyWindow(t *testing.T) {
	v, clk := newHarness(t)
	_, _, rev := testkeys.CI()
	before := time.Date(2026, 5, 1, 0, 0, 0, 0, time.UTC)
	after := time.Date(2026, 6, 2, 0, 0, 0, 0, time.UTC)

	st := statement(before, rev.ID)
	clk.set(before)
	expectCode(t, v, sign(t, st, rev.Private), "")

	st2 := statement(after, rev.ID)
	clk.set(after)
	expectCode(t, v, sign(t, st2, rev.Private), attestation.CodeKeyRevoked)
}

// Unknown key / key belonging to another builder.
func TestForeignKey(t *testing.T) {
	v, clk := newHarness(t)
	cur, _, _ := testkeys.CI()
	other := testkeys.Other()

	// Key id unknown to policy.
	st := statement(clk.get(), "key-nope")
	expectCode(t, v, sign(t, st, cur.Private), attestation.CodeUnknownKey)

	// Known key but bound to a different builder id.
	st2 := statement(clk.get(), other.ID)
	expectCode(t, v, sign(t, st2, other.Private), attestation.CodeUnknownKey)
}

// Valid signature, but builder / source not in policy.
func TestPolicyMismatch(t *testing.T) {
	v, clk := newHarness(t)
	cur, _, _ := testkeys.CI()

	st := statement(clk.get(), cur.ID)
	st.BuilderID = "rogue.example.org"
	expectCode(t, v, sign(t, st, cur.Private), attestation.CodeUnknownBuilder)

	st2 := statement(clk.get(), cur.ID)
	st2.Source.Repository = "https://github.com/evil-org/app"
	expectCode(t, v, sign(t, st2, cur.Private), attestation.CodeUntrustedSource)
}

// Freshness window.
func TestFreshnessWindow(t *testing.T) {
	v, clk := newHarness(t)
	cur, _, _ := testkeys.CI()
	now := clk.get()

	tooOld := statement(now.Add(-6*time.Minute), cur.ID)
	expectCode(t, v, sign(t, tooOld, cur.Private), attestation.CodeFreshness)

	tooNew := statement(now.Add(6*time.Minute), cur.ID)
	expectCode(t, v, sign(t, tooNew, cur.Private), attestation.CodeFreshness)

	edge := statement(now.Add(4*time.Minute+59*time.Second), cur.ID)
	expectCode(t, v, sign(t, edge, cur.Private), "")
}

// Replay: the same nonce can be presented only once.
func TestReplay(t *testing.T) {
	v, clk := newHarness(t)
	cur, _, _ := testkeys.CI()
	raw := sign(t, statement(clk.get(), cur.ID), cur.Private)
	expectCode(t, v, raw, "")
	expectCode(t, v, raw, attestation.CodeReplay)
}

// Cross-purpose signature: same key, different domain prefix.
func TestCrossPurpose(t *testing.T) {
	v, clk := newHarness(t)
	cur, _, _ := testkeys.CI()
	env, err := attestation.SignWithPrefix(statement(clk.get(), cur.ID), cur.Private, "OTHER_PROTOCOL/v2\n")
	if err != nil {
		t.Fatal(err)
	}
	expectCode(t, v, marshalEnvelope(t, env), attestation.CodeBadSignature)
}

// payloadType pin.
func TestPayloadTypeMismatch(t *testing.T) {
	v, clk := newHarness(t)
	cur, _, _ := testkeys.CI()
	env, err := attestation.Sign(statement(clk.get(), cur.ID), cur.Private)
	if err != nil {
		t.Fatal(err)
	}
	env.PayloadType = "application/vnd.something-else"
	expectCode(t, v, marshalEnvelope(t, env), attestation.CodeCrossPurpose)
}

// Extra field inside the statement is rejected even if the signature covers it.
func TestExtraField(t *testing.T) {
	v, clk := newHarness(t)
	cur, _, _ := testkeys.CI()
	st := statement(clk.get(), cur.ID)
	payload, err := attestation.CanonicalizeStatement(st)
	if err != nil {
		t.Fatal(err)
	}
	// Decode to a generic map, add the extra field, re-encode canonically and
	// sign: the signature is valid over exactly these bytes, but the declared
	// schema forbids unknown fields.
	parsed, pErr := canonical.Parse(payload)
	if pErr != nil {
		t.Fatal(pErr)
	}
	parsed.(map[string]any)["debugNote"] = "unexpected but signed"
	patched, pErr := canonical.Marshal(parsed)
	if pErr != nil {
		t.Fatal(pErr)
	}
	sig := ed25519.Sign(cur.Private, attestation.SigningInput(patched))
	env := map[string]any{
		"payloadType": attestation.PayloadType,
		"payload":     base64.RawURLEncoding.EncodeToString(patched),
		"signature":   base64.RawURLEncoding.EncodeToString(sig),
	}
	raw, _ := json.Marshal(env)
	expectCode(t, v, raw, attestation.CodeStatementInvalid)
}

// Non-canonical wire bytes with a valid signature over canonical bytes.
func TestNonCanonicalWire(t *testing.T) {
	v, clk := newHarness(t)
	cur, _, _ := testkeys.CI()
	env, err := attestation.Sign(statement(clk.get(), cur.ID), cur.Private)
	if err != nil {
		t.Fatal(err)
	}
	payload, _ := base64.RawURLEncoding.DecodeString(env.Payload)
	// Insert whitespace: same logical document, different signed bytes.
	nonCanonical := []byte(strings.Replace(string(payload), `","`, `", "`, 1))
	env.Payload = base64.RawURLEncoding.EncodeToString(nonCanonical)
	expectCode(t, v, marshalEnvelope(t, env), attestation.CodeNonCanonical)
}

// Duplicate keys at envelope or statement level.
func TestDuplicateKeys(t *testing.T) {
	v, _ := newHarness(t)
	rawDup := []byte(`{"payloadType":"x","payloadType":"y","payload":"z","signature":"z"}`)
	expectCode(t, v, rawDup, attestation.CodeDuplicateKey)

	inner := []byte(`{"_type":"build-attestation/statement/v1","_type":"x"}`)
	env := map[string]any{
		"payloadType": attestation.PayloadType,
		"payload":     base64.RawURLEncoding.EncodeToString(inner),
		"signature":   base64.RawURLEncoding.EncodeToString(make([]byte, ed25519.SignatureSize)),
	}
	raw, _ := json.Marshal(env)
	expectCode(t, v, raw, attestation.CodeDuplicateKey)
}

func TestMalformedInputs(t *testing.T) {
	v, _ := newHarness(t)
	bad := [][]byte{
		nil,
		[]byte(`not json`),
		[]byte(`[]`),
		[]byte(`{"payloadType":"x"}`),
		[]byte(`{"payloadType":"x","payload":"%%%","signature":"z","extra":1}`),
	}
	for _, raw := range bad {
		res, err := v.Verify(raw)
		if err == nil {
			t.Fatalf("expected rejection for %s, got %+v", raw, res)
		}
	}
}

func TestStatementShapeValidation(t *testing.T) {
	v, clk := newHarness(t)
	cur, _, _ := testkeys.CI()
	base := func() *attestation.Statement { return statement(clk.get(), cur.ID) }

	cases := []struct {
		name string
		mut  func(*attestation.Statement)
	}{
		{"bad type", func(s *attestation.Statement) { s.Type = "other/v1" }},
		{"bad digest alg", func(s *attestation.Statement) { s.Subjects[0].Alg = "sha512" }},
		{"uppercase digest", func(s *attestation.Statement) { s.Subjects[0].Value = strings.ToUpper(s.Subjects[0].Value) }},
		{"no subjects", func(s *attestation.Statement) { s.Subjects = nil }},
		{"bad commit", func(s *attestation.Statement) { s.Source.Commit = "deadbeef" }},
		{"short nonce", func(s *attestation.Statement) { s.Nonce = "short" }},
		{"non-utc timestamp", func(s *attestation.Statement) { s.IssuedAt = "2026-09-20T12:00:00+08:00" }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			st := base()
			c.mut(st)
			env, err := attestation.Sign(st, cur.Private)
			if err != nil {
				// Some shapes are rejected at signing time — that's fine.
				if err.Code == attestation.CodeCrossPurpose || err.Code == attestation.CodeStatementInvalid {
					return
				}
				t.Fatalf("unexpected signing error: %v", err)
			}
			expectCode(t, v, marshalEnvelope(t, env), attestation.CodeStatementInvalid)
		})
	}
}
