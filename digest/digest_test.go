package digest

import "testing"

func TestParseAndRoundTrip(t *testing.T) {
	d := NewSHA256([]byte("hello model cache"))
	s := d.String()
	parsed, err := Parse(s)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if parsed != d {
		t.Fatalf("round trip mismatch: %s vs %s", parsed, d)
	}
}

func TestParseRejectsBad(t *testing.T) {
	bad := []string{
		"",
		"noseparator",
		"md5:0123456789abcdef0123456789abcdef",
		"sha256:tooshort",
		"sha256:zzz444444444444444444444444444444444444444444444444444444444",
	}
	for _, b := range bad {
		if _, err := Parse(b); err == nil {
			t.Errorf("Parse(%q) unexpectedly succeeded", b)
		}
	}
}

func TestUnmarshalText(t *testing.T) {
	var d Digest
	if err := d.UnmarshalText([]byte(NewSHA256([]byte("x")).String())); err != nil {
		t.Fatal(err)
	}
	if d.IsZero() {
		t.Fatal("expected non-zero digest")
	}
}
