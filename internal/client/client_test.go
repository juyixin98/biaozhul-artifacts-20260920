package client

import (
	"bytes"
	"errors"
	"testing"
)

func TestParseContentRange(t *testing.T) {
	start, end, total, err := parseContentRange("bytes 0-99/1024")
	if err != nil || start != 0 || end != 99 || total != 1024 {
		t.Fatalf("got %d-%d/%d, %v", start, end, total, err)
	}
	_, _, total, err = parseContentRange("bytes 10-20/*")
	if err != nil || total != -1 {
		t.Fatalf("star total: total=%d err=%v", total, err)
	}
	for _, bad := range []string{
		"", "bytes ", "bytes 0-99", "bytes x-9/10", "bytes 9-0/10",
		"items 0-9/10", "bytes 0-99/", "bytes 0-/10",
	} {
		if _, _, _, err := parseContentRange(bad); err == nil {
			t.Errorf("parseContentRange(%q) expected error", bad)
		}
	}
}

func TestReassemble(t *testing.T) {
	original := []byte("0123456789")
	parts := []RangePart{
		{Start: 0, End: 3, Total: 10, Payload: []byte("0123")},
		{Start: 4, End: 9, Total: 10, Payload: []byte("456789")},
	}
	got, err := Reassemble(parts, 10)
	if err != nil {
		t.Fatalf("Reassemble: %v", err)
	}
	if err := Verify(got, original); err != nil {
		t.Fatalf("Verify: %v", err)
	}
}

func TestReassembleGap(t *testing.T) {
	parts := []RangePart{
		{Start: 0, End: 1, Payload: []byte("01")},
		{Start: 4, End: 5, Payload: []byte("45")},
	}
	_, err := Reassemble(parts, 6)
	if !errors.Is(err, ErrGap) {
		t.Fatalf("want ErrGap, got %v", err)
	}
}

func TestReassembleOverlap(t *testing.T) {
	parts := []RangePart{
		{Start: 0, End: 2, Payload: []byte("012")},
		{Start: 2, End: 4, Payload: []byte("234")},
	}
	_, err := Reassemble(parts, 5)
	if !errors.Is(err, ErrOverlap) {
		t.Fatalf("want ErrOverlap, got %v", err)
	}
}

func TestVerifyPartsOverlapTolerated(t *testing.T) {
	original := []byte("abcdefghij")
	parts := []RangePart{
		{Start: 0, End: 4, Payload: []byte("abcde")},
		{Start: 3, End: 9, Payload: []byte("defghij")},
	}
	cov, err := VerifyParts(parts, original)
	if err != nil {
		t.Fatalf("overlapping but correct parts: %v", err)
	}
	if !cov.Complete() {
		t.Errorf("expected complete coverage, gaps=%d", cov.Gaps)
	}
}

func TestVerifyPartsDetectsWrongByte(t *testing.T) {
	parts := []RangePart{{Start: 0, End: 2, Payload: []byte("axc")}}
	_, err := VerifyParts(parts, []byte("abc"))
	if !errors.Is(err, ErrIntegrity) {
		t.Fatalf("want ErrIntegrity, got %v", err)
	}
}

func TestVerifyLengthMismatch(t *testing.T) {
	if err := Verify([]byte("a"), []byte("ab")); !errors.Is(err, ErrIntegrity) {
		t.Fatalf("want ErrIntegrity, got %v", err)
	}
	if err := Verify([]byte("ab"), []byte("ab")); err != nil {
		t.Fatalf("equal: %v", err)
	}
	if got := SHA256(nil); len(got) != 64 {
		t.Errorf("sha256 len = %d", len(got))
	}
}

func TestParseMultipartRejectsWrongPayloadLength(t *testing.T) {
	// Hand-build a body claiming 2 bytes while delivering 1.
	body := "--B\r\n" +
		"Content-Range: bytes 0-1/3\r\n\r\n" +
		"x\r\n" +
		"--B--\r\n"
	_, err := parseMultipart([]byte(body), "multipart/byteranges; boundary=B")
	if err == nil || !bytes.Contains([]byte(err.Error()), []byte("payload length")) {
		t.Fatalf("want payload length error, got %v", err)
	}
}
