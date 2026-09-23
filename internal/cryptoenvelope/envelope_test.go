package cryptoenvelope_test

import (
	"crypto/ed25519"
	"encoding/json"
	"strings"
	"testing"

	"ximbox/internal/cryptoenvelope"
)

func mustSign(t *testing.T, key cryptoenvelope.FixtureKey, chain, channel, block string, seq uint64, body string) *cryptoenvelope.Envelope {
	t.Helper()
	env, err := cryptoenvelope.Sign(key.Private, chain, channel, block, seq, json.RawMessage(body))
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	return env
}

func TestSignVerifyRoundTrip(t *testing.T) {
	key := cryptoenvelope.FixtureKeyFor(cryptoenvelope.ChainA)
	env := mustSign(t, key, cryptoenvelope.ChainA, "ch1", cryptoenvelope.ZeroHash, 7,
		`{"type":"transfer","to":"alice","amount":42}`)

	if err := env.Verify(key.Public); err != nil {
		t.Fatalf("verify failed on intact envelope: %v", err)
	}
	// Digest is the real SHA-256 of the exact body bytes.
	if want := cryptoenvelope.DigestBody(env.Body); want != env.BodyDigest {
		t.Fatalf("digest mismatch %s != %s", want, env.BodyDigest)
	}
	if len(env.Signature) != 2+ed25519.SignatureSize*2 {
		t.Fatalf("signature has wrong encoding length %d", len(env.Signature))
	}
}

func TestTamperedBodyRejected(t *testing.T) {
	key := cryptoenvelope.FixtureKeyFor(cryptoenvelope.ChainA)
	env := mustSign(t, key, cryptoenvelope.ChainA, "ch1", cryptoenvelope.ZeroHash, 0,
		`{"type":"transfer","to":"alice","amount":10}`)

	// Mutate the body without recomputing the digest: must fail the digest check.
	env.Body = json.RawMessage(`{"type":"transfer","to":"alice","amount":999}`)
	if err := env.Verify(key.Public); err == nil {
		t.Fatal("tampered body (stale digest) accepted")
	}

	// Recompute the digest too: now the digest check passes but the signature
	// over the old digest fails. This proves the body is cryptographically bound.
	env.BodyDigest = cryptoenvelope.DigestBody(env.Body)
	if err := env.Verify(key.Public); !errorsIs(err, cryptoenvelope.ErrInvalidSignature) {
		t.Fatalf("expected invalid signature, got %v", err)
	}
}

func TestTamperedKeyOrAnchorRejected(t *testing.T) {
	key := cryptoenvelope.FixtureKeyFor(cryptoenvelope.ChainA)
	base := mustSign(t, key, cryptoenvelope.ChainA, "ch1", cryptoenvelope.ZeroHash, 5,
		`{"type":"transfer","to":"alice","amount":10}`)

	cases := map[string]func(e *cryptoenvelope.Envelope){
		"sequence":  func(e *cryptoenvelope.Envelope) { e.Sequence = 6 },
		"channel":   func(e *cryptoenvelope.Envelope) { e.Channel = "ch2" },
		"chain":     func(e *cryptoenvelope.Envelope) { e.SourceChain = cryptoenvelope.ChainB },
		"blockhash": func(e *cryptoenvelope.Envelope) { e.BlockHash = "0x" + strings.Repeat("b", 64) },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			e := *base
			mutate(&e)
			if err := e.Verify(key.Public); err == nil {
				t.Fatalf("mutation %q accepted", name)
			}
		})
	}
}

func TestWrongTrustedKeyRejected(t *testing.T) {
	keyA := cryptoenvelope.FixtureKeyFor(cryptoenvelope.ChainA)
	keyB := cryptoenvelope.FixtureKeyFor(cryptoenvelope.ChainB)
	env := mustSign(t, keyA, cryptoenvelope.ChainA, "ch1", cryptoenvelope.ZeroHash, 0,
		`{"type":"transfer","to":"alice","amount":10}`)
	if err := env.Verify(keyB.Public); err == nil {
		t.Fatal("envelope verified against a different chain's trusted key")
	}
}

func TestDeterministicFixtures(t *testing.T) {
	// The fixture keys are deterministic and publicly known; both derivations
	// must be byte-identical so recorded examples verify on every machine.
	k1 := cryptoenvelope.FixtureKeyFor(cryptoenvelope.ChainA)
	k2 := cryptoenvelope.FixtureKeyFor(cryptoenvelope.ChainA)
	if !k1.Public.Equal(k2.Public) {
		t.Fatal("fixture key derivation is not deterministic")
	}
	if k1.Public.Equal(cryptoenvelope.FixtureKeyFor(cryptoenvelope.ChainB).Public) {
		t.Fatal("chainA and chainB fixture keys collide")
	}
}

func TestSigningBytesCanonical(t *testing.T) {
	b1, err := cryptoenvelope.SigningBytes("chainA", "ch", cryptoenvelope.ZeroHash, 1, "0xabc")
	if err != nil {
		t.Fatal(err)
	}
	b2, _ := cryptoenvelope.SigningBytes("chainA", "ch", cryptoenvelope.ZeroHash, 1, "0xabc")
	if string(b1) != string(b2) {
		t.Fatal("signing bytes are not deterministic")
	}
	if !strings.Contains(string(b1), `"block_hash"`) {
		t.Fatalf("signed document must bind the block anchor: %s", b1)
	}
}

// TestEnvelopeSurvivesJSONTransport proves the digest/signature still verify
// after the envelope goes through indented marshalling and a fresh JSON
// unmarshal (which re-encodes the nested body as compact bytes).
func TestEnvelopeSurvivesJSONTransport(t *testing.T) {
	key := cryptoenvelope.FixtureKeyFor(cryptoenvelope.ChainA)
	// Body intentionally supplied with arbitrary whitespace/key order.
	env, err := cryptoenvelope.Sign(key.Private, cryptoenvelope.ChainA, "ch",
		cryptoenvelope.ZeroHash, 3,
		json.RawMessage(`{ "amount" : 50 , "to":"alice" , "type":"transfer" }`))
	if err != nil {
		t.Fatal(err)
	}
	wire, err := json.MarshalIndent(env, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	var got cryptoenvelope.Envelope
	if err := json.Unmarshal(wire, &got); err != nil {
		t.Fatal(err)
	}
	if err := got.Verify(key.Public); err != nil {
		t.Fatalf("verification failed after JSON transport round-trip: %v", err)
	}
	if string(got.Body) != `{"amount":50,"to":"alice","type":"transfer"}` {
		t.Fatalf("body was not canonicalized on the wire: %s", got.Body)
	}
}

// TestSemanticallyIdenticalBodiesSameDigest: whitespace/key order changes the
// raw bytes but must NOT change the digest (same semantic JSON).
func TestSemanticallyIdenticalBodiesSameDigest(t *testing.T) {
	a := cryptoenvelope.DigestBody([]byte(`{"to":"alice","amount":1}`))
	b := cryptoenvelope.DigestBody([]byte(`{ "amount": 1, "to": "alice" }`))
	if a != b {
		t.Fatalf("equivalent JSON produced different digests: %s vs %s", a, b)
	}
	c := cryptoenvelope.DigestBody([]byte(`{"to":"bob","amount":1}`))
	if a == c {
		t.Fatal("semantically different JSON produced the same digest")
	}
}

// errorsIs is a tiny local errors.Is wrapper to keep imports minimal.
func errorsIs(err, target error) bool {
	if err == nil {
		return false
	}
	return err == target || strings.Contains(err.Error(), target.Error())
}

var _ ed25519.PublicKey
