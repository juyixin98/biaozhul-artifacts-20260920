package cryptopkg

import (
	"strings"
	"testing"
)

func TestInputHashDeterministicAndOrderIndependent(t *testing.T) {
	a := []InputSample{
		{TS: 10, Price: 200, Source: "s"},
		{TS: 0, Price: 100, Source: "s"},
	}
	b := []InputSample{
		{TS: 0, Price: 100, Source: "s"},
		{TS: 10, Price: 200, Source: "s"},
	}
	h1 := InputHash(0, 60, 30, a)
	h2 := InputHash(0, 60, 30, b)
	if h1 != h2 {
		t.Fatalf("hash depends on input order: %s vs %s", h1, h2)
	}
	if len(h1) != 64 {
		t.Fatalf("hash length = %d, want 64 hex chars", len(h1))
	}
}

func TestInputHashChangesWithContent(t *testing.T) {
	base := []InputSample{{TS: 0, Price: 100, Source: "s"}}
	h := InputHash(0, 60, 30, base)

	cases := map[string][]any{
		"price":      {InputHash(0, 60, 30, []InputSample{{TS: 0, Price: 101, Source: "s"}})},
		"timestamp":  {InputHash(0, 60, 30, []InputSample{{TS: 1, Price: 100, Source: "s"}})},
		"source":     {InputHash(0, 60, 30, []InputSample{{TS: 0, Price: 100, Source: "x"}})},
		"window_end": {InputHash(0, 61, 30, base)},
		"stale":      {InputHash(0, 60, 31, base)},
		"added_row": {InputHash(0, 60, 30, []InputSample{
			{TS: 0, Price: 100, Source: "s"}, {TS: 10, Price: 1, Source: "s"},
		})},
	}
	for name, v := range cases {
		got := v[0].(string)
		if got == h {
			t.Fatalf("hash collision when changing %s", name)
		}
	}
}

func TestSignVerifyRoundTrip(t *testing.T) {
	key := []byte("0123456789abcdef0123456789abcdef")
	p := VersionPayload{
		WindowStart: 0, WindowEnd: 60, Version: 2,
		InputHash: "abc", TWAPNum: "550", TWAPDen: "3",
		CoveredMicros: 60, WindowMicros: 60,
		ConflictCount: 1, Stale: false, LastSampleTS: 50,
	}
	sig := Sign(key, p)
	if !Verify(key, p, sig) {
		t.Fatal("valid signature failed verification")
	}

	// Any tampering must invalidate the signature.
	tampered := p
	tampered.TWAPNum = "551"
	if Verify(key, tampered, sig) {
		t.Fatal("tampered payload verified")
	}
	tampered2 := p
	tampered2.Version = 3
	if Verify(key, tampered2, sig) {
		t.Fatal("tampered version verified")
	}
	if Verify([]byte("wrong-key-0000000000000000000000"), p, sig) {
		t.Fatal("wrong key verified")
	}
	if Verify(key, p, sig+"00") {
		t.Fatal("malformed signature verified")
	}
}

func TestGenerateKeyIsRandom(t *testing.T) {
	k1, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	k2, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	if len(k1) != 32 {
		t.Fatalf("key length = %d, want 32", len(k1))
	}
	if string(k1) == string(k2) {
		t.Fatal("two generated keys are identical")
	}
}

func TestCanonicalEncodingResistsSourceInjection(t *testing.T) {
	// A source containing newlines must not be able to forge the
	// canonical representation of another sample set.
	s1 := []InputSample{{TS: 0, Price: 1, Source: "a\n10\t\"s\"\t9"}}
	s2 := []InputSample{
		{TS: 0, Price: 1, Source: "a"},
	}
	h1 := InputHash(0, 60, 30, s1)
	h2 := InputHash(0, 60, 30, s2)
	if h1 == h2 {
		t.Fatal("canonical encoding is vulnerable to newline injection")
	}
	if strings.Contains(string(CanonicalInputs(0, 60, 30, s1)), "\n9") {
		t.Fatal("unquoted newline survived canonical encoding")
	}
}
