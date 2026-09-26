package mpart

import (
	"bytes"
	"testing"

	"httprange/internal/rangespec"
)

func TestWriteRoundTrip(t *testing.T) {
	parts := []Part{
		{ContentType: "application/octet-stream",
			Range: rangespec.Resolved{Start: 0, End: 3}, Total: 10,
			Payload: []byte("abcd")},
		{ContentType: "application/octet-stream",
			Range: rangespec.Resolved{Start: 8, End: 9}, Total: 10,
			Payload: []byte("ij")},
	}
	boundary := NewBoundary()
	var buf bytes.Buffer
	if err := Write(&buf, parts, boundary); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if int64(buf.Len()) != Size(parts, boundary) {
		t.Errorf("Size() = %d, actual %d", Size(parts, boundary), buf.Len())
	}
	for _, want := range []string{
		"--" + boundary + "\r\n",
		"Content-Range: bytes 0-3/10\r\n",
		"Content-Range: bytes 8-9/10\r\n",
		"--" + boundary + "--\r\n",
	} {
		if !bytes.Contains(buf.Bytes(), []byte(want)) {
			t.Errorf("body missing %q", want)
		}
	}
}

func TestBoundaryDistinctness(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 50; i++ {
		b := NewBoundary()
		if seen[b] {
			t.Fatalf("duplicate boundary %q", b)
		}
		seen[b] = true
	}
}

func TestMediaType(t *testing.T) {
	if got := MediaType("b1"); got != "multipart/byteranges; boundary=b1" {
		t.Errorf("MediaType = %q", got)
	}
}
