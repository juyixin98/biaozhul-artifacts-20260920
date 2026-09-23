package ws

import (
	"math/rand"
	"testing"
	"unicode/utf8"
)

// TestUTF8ValidatorRandom cross-checks the DFA validator against the standard
// library's utf8.Valid over thousands of random byte sequences (including
// valid UTF-8, truncated sequences and arbitrary invalid bytes).
func TestUTF8ValidatorRandom(t *testing.T) {
	rng := rand.New(rand.NewSource(20260923))
	for iter := 0; iter < 20000; iter++ {
		var s []byte
		switch iter % 4 {
		case 0, 1:
			// Arbitrary random bytes: valid or not.
			s = make([]byte, rng.Intn(40))
			rng.Read(s)
		case 2:
			// Valid UTF-8 built from random runes.
			n := rng.Intn(20)
			for i := 0; i < n; i++ {
				r := rune(1 + rng.Intn(0x10FFFF))
				if r >= 0xD800 && r <= 0xDFFF {
					r = 'x' // surrogates are not valid Unicode
				}
				buf := make([]byte, utf8.RuneLen(r))
				utf8.EncodeRune(buf, r)
				s = append(s, buf...)
			}
		case 3:
			// A valid string truncated at a random point: a cut inside a
			// multi-byte sequence must be rejected by End but never crash.
			base := []byte("αβγ你好𠀀𪛖 mixed ASCII")
			if len(base) == 0 {
				continue
			}
			s = base[:rng.Intn(len(base)+1)]
		}

		var v utf8Validator
		ok := v.write(s)
		got := ok && v.end()
		want := utf8.Valid(s)
		if got != want {
			t.Fatalf("iter %d: validator=%v utf8.Valid=%v bytes=% x", iter, got, want, s)
		}
	}
}

// TestUTF8ValidatorIncremental verifies that splitting one byte stream at every
// possible boundary gives the same verdict as one write.
func TestUTF8ValidatorIncremental(t *testing.T) {
	samples := [][]byte{
		[]byte("hello"),
		[]byte("你好，世界"),
		[]byte{0x41, 0xE4, 0xBD, 0xA0, 0x42},
		{0xF0, 0xA0, 0x9C, 0x96}, // U+20716
	}
	for _, s := range samples {
		if !utf8.Valid(s) {
			t.Fatalf("test bug: sample must be valid: % x", s)
		}
		for cut := 0; cut <= len(s); cut++ {
			var v utf8Validator
			if !v.write(s[:cut]) || !v.write(s[cut:]) || !v.end() {
				t.Fatalf("valid string rejected when split at %d: % x", cut, s)
			}
		}
	}
}
