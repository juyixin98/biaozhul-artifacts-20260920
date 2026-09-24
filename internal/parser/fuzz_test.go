package parser

import "testing"

// FuzzParser checks two invariants on arbitrary byte streams:
//  1. the parser never panics;
//  2. every two-way byte split yields the same result as the one-shot parse.
func FuzzParser(f *testing.F) {
	seeds := [][]byte{
		req("GET / HTTP/1.1", "Host: x"),
		append(req("POST / HTTP/1.1", "Content-Length: 3"), []byte("abc")...),
		append(req("POST / HTTP/1.1", "Transfer-Encoding: chunked"),
			[]byte("3\r\nabc\r\n0\r\n\r\n")...),
		req("POST / HTTP/1.1", "Content-Length: 1", "Transfer-Encoding: chunked"),
		[]byte("GET / HTTP/1.1\r\nX: a\r\n b\r\n\r\n"),
		[]byte("POST / HTTP/1.1\nContent-Length: 1\r\n\r\na"),
	}
	for _, s := range seeds {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, input []byte) {
		// Bound input so exhaustive splits stay fast inside the fuzzer.
		if len(input) > 256 {
			input = input[:256]
		}
		if m := VerifyAllSplits(input); m != nil {
			t.Fatalf("split inconsistency: %v", m)
		}
	})
}
