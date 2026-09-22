package domainname

import "testing"

func TestNormalize(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"example.com", "example.com"},
		{"ExAmPle.COM", "example.com"},      // case-insensitive
		{"example.com.", "example.com"},     // trailing dot (absolute form)
		{"  example.com  ", "example.com"},  // whitespace
		{"münchen.de", "xn--mnchen-3ya.de"}, // IDN -> punycode
		{"XN--MNCHEN-3YA.DE", "xn--mnchen-3ya.de"},
		{"a.b.co.uk", "a.b.co.uk"},
	}
	for _, c := range cases {
		got, err := Normalize(c.in)
		if err != nil {
			t.Fatalf("Normalize(%q): %v", c.in, err)
		}
		if got != c.want {
			t.Errorf("Normalize(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestNormalizeEquivalentSpellingsCollide(t *testing.T) {
	spellings := []string{"Example.COM.", "example.com", "EXAMPLE.COM."}
	first, err := Normalize(spellings[0])
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range spellings[1:] {
		got, err := Normalize(s)
		if err != nil {
			t.Fatal(err)
		}
		if got != first {
			t.Errorf("%q normalizes to %q, expected collision on %q", s, got, first)
		}
	}
}

func TestNormalizeRejects(t *testing.T) {
	for _, in := range []string{
		"", ".", "com", "-bad.com", "bad-.com", "a..com",
		"under_score.com", string(make([]byte, 300)) + ".com",
	} {
		if _, err := Normalize(in); err == nil {
			t.Errorf("Normalize(%q) should fail", in)
		}
	}
}

func TestTLD(t *testing.T) {
	if got := TLD("a.b.co.uk"); got != "uk" {
		t.Errorf("TLD = %q, want uk", got)
	}
	if got := TLD("xn--mnchen-3ya.de"); got != "de" {
		t.Errorf("TLD = %q, want de", got)
	}
}
