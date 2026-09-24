package sse

import (
	"bytes"
	"io"
	"strings"
	"testing"
)

func TestWriteEventMultiline(t *testing.T) {
	var buf bytes.Buffer
	if err := WriteEvent(&buf, 42, "message", "line one\nline two\n"); err != nil {
		t.Fatal(err)
	}
	want := "id: 42\nevent: message\ndata: line one\ndata: line two\ndata:\n\n"
	if buf.String() != want {
		t.Fatalf("multiline encoding mismatch:\n got: %q\nwant: %q", buf.String(), want)
	}
}

func TestWriteEventSimpleAndEmpty(t *testing.T) {
	var b bytes.Buffer
	if err := WriteEvent(&b, 7, "", "hello"); err != nil {
		t.Fatal(err)
	}
	if got, want := b.String(), "id: 7\ndata: hello\n\n"; got != want {
		t.Fatalf("got %q want %q", got, want)
	}

	b.Reset()
	if err := WriteEvent(&b, 8, "", ""); err != nil {
		t.Fatal(err)
	}
	if got, want := b.String(), "id: 8\ndata:\n\n"; got != want {
		t.Fatalf("empty data: got %q want %q", got, want)
	}
}

func TestControlEventHasNoID(t *testing.T) {
	var b bytes.Buffer
	if err := WriteControlEvent(&b, "reset", `{"reason":"expired"}`); err != nil {
		t.Fatal(err)
	}
	got := b.String()
	if strings.Contains(got, "id:") {
		t.Fatalf("control frame must not carry id, got: %q", got)
	}
	if !strings.HasPrefix(got, "event: reset\n") || !strings.HasSuffix(got, "\n\n") {
		t.Fatalf("bad control frame: %q", got)
	}
}

func TestNormalizeData(t *testing.T) {
	cases := map[string]string{
		"plain":       "plain",
		"a\nb":        "a\nb",
		"a\rb\r\nc\n": "a\nb\nc\n",
		"\r\r":        "\n\n",
	}
	for in, want := range cases {
		if got := NormalizeData(in); got != want {
			t.Errorf("NormalizeData(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestDecoderRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	// Comment + two events, second one multiline; decoder must join data
	// lines with "\n" and ignore the heartbeat.
	_ = WriteComment(&buf, "hb")
	_ = WriteEvent(&buf, 1, "message", "first")
	_ = WriteEvent(&buf, 2, "message", "a\nb\nc")

	dec := NewDecoder(&buf)
	ev1, err := dec.Next()
	if err != nil {
		t.Fatal(err)
	}
	if ev1.ID != "1" || ev1.Data != "first" || ev1.Type != "message" {
		t.Fatalf("event1 decoded wrong: %+v", ev1)
	}
	ev2, err := dec.Next()
	if err != nil {
		t.Fatal(err)
	}
	if ev2.ID != "2" || ev2.Data != "a\nb\nc" {
		t.Fatalf("multiline event decoded wrong: %+v", ev2)
	}
	if _, err := dec.Next(); err != io.EOF {
		t.Fatalf("want EOF, got %v", err)
	}
}

func TestDecoderIDBufferPersistsAndCRLF(t *testing.T) {
	// CRLF line endings and an event with no id: decoder keeps the prior id.
	stream := "id: 5\r\ndata: a\r\n\r\ndata: b\r\n\r\n"
	dec := NewDecoder(strings.NewReader(stream))
	ev1, err := dec.Next()
	if err != nil {
		t.Fatal(err)
	}
	ev2, err := dec.Next()
	if err != nil {
		t.Fatal(err)
	}
	if ev1.ID != "5" || ev2.ID != "5" {
		t.Fatalf("id buffer must persist, got %q and %q", ev1.ID, ev2.ID)
	}
	if ev1.Data != "a" || ev2.Data != "b" {
		t.Fatalf("data mismatch: %q %q", ev1.Data, ev2.Data)
	}
}

func TestDecoderTruncatedFrameNotDispatched(t *testing.T) {
	// Complete event followed by a frame cut before its terminating blank
	// line: only the complete event may be delivered (no phantom event).
	stream := "id: 1\ndata: ok\n\nid: 2\ndata: partial\n"
	dec := NewDecoder(strings.NewReader(stream))
	ev, err := dec.Next()
	if err != nil {
		t.Fatal(err)
	}
	if ev.ID != "1" {
		t.Fatalf("got %q want 1", ev.ID)
	}
	if _, err := dec.Next(); err != io.EOF {
		t.Fatalf("truncated frame must not dispatch; want EOF, got %v", err)
	}
}
