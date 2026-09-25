package stream

import (
	"bytes"
	"io"
	"strings"
	"testing"
)

func TestEncodeDecodeRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	want := []Record{
		Item(1, []byte("aaa")),
		Item(2, []byte("bbb")),
		Error(CodeUpstreamFailure, "boom", 2),
	}
	for _, r := range want {
		if err := Encode(&buf, r); err != nil {
			t.Fatalf("Encode: %v", err)
		}
	}

	dec := NewDecoder(&buf, 1<<20)
	for i, w := range want {
		got, err := dec.Next()
		if err != nil {
			t.Fatalf("Next(%d): %v", i, err)
		}
		if got != w {
			t.Fatalf("record %d = %+v, want %+v", i, got, w)
		}
	}
	if _, err := dec.Next(); err != io.EOF {
		t.Fatalf("final Next = %v, want io.EOF", err)
	}
}

func TestDecodeRejectsOversizedLine(t *testing.T) {
	big := strings.Repeat("x", 4096)
	line := `{"type":"item","seq":1,"payload":"` + big + `"}` + "\n"
	dec := NewDecoder(strings.NewReader(line), 1024)
	if _, err := dec.Next(); err == nil {
		t.Fatal("oversized line accepted")
	}
}

func TestDecodeRejectsGarbage(t *testing.T) {
	dec := NewDecoder(strings.NewReader("not json\n"), 1024)
	if _, err := dec.Next(); err == nil {
		t.Fatal("garbage line accepted")
	}
}

func TestTrailerConstructors(t *testing.T) {
	e := End(7)
	if e.Type != TypeEnd || e.Sent != 7 {
		t.Fatalf("End = %+v", e)
	}
	er := Error(CodeServerShutdown, "bye", 3)
	if er.Type != TypeError || er.Code != CodeServerShutdown || er.Sent != 3 {
		t.Fatalf("Error = %+v", er)
	}
}
