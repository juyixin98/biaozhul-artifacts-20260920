package canonical

import (
	"errors"
	"testing"
)

func TestRoundTrip(t *testing.T) {
	cases := map[string]string{
		"empty object":           `{}`,
		"primitives":             `{"a":null,"b":true,"c":false,"d":"str","e":0,"f":-1,"g":1234567890}`,
		"nested":                 `{"z":[3,2,1],"a":{"y":1,"x":[true,null]}}`,
		"unicode passes through": `{"名前":"日本語 🚀"}`,
		"escapes":                `{"q":"\"\\\n\r\t\b\f\u0001/"}`,
		"empty array":            `{"a":[]}`,
		"deep nesting":           `{"a":{"b":{"c":{"d":-42}}}}`,
	}
	for name, in := range cases {
		t.Run(name, func(t *testing.T) {
			v, err := Parse([]byte(in))
			if err != nil {
				t.Fatalf("parse %q: %v", in, err)
			}
			out, err := Marshal(v)
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			v2, err := Parse(out)
			if err != nil {
				t.Fatalf("re-parse canonical %q: %v", out, err)
			}
			out2, _ := Marshal(v2)
			if string(out) != string(out2) {
				t.Fatalf("not idempotent:\n got %q\nwant %q", out2, out)
			}
		})
	}
}

func TestKeyOrderingAndSpacing(t *testing.T) {
	v, err := Parse([]byte(`{"b":1,"a":2,"c":{"z":1,"a":2}}`))
	if err != nil {
		t.Fatal(err)
	}
	out, err := Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"a":2,"b":1,"c":{"a":2,"z":1}}`
	if string(out) != want {
		t.Fatalf("got  %q\nwant %q", out, want)
	}
}

func TestRejectDuplicateKeys(t *testing.T) {
	inputs := []string{
		`{"a":1,"a":2}`,
		`{"x":{"y":1,"y":2}}`,
		`{"a":1,"b":{"c":1,"c":2}}`,
	}
	for _, in := range inputs {
		_, err := Parse([]byte(in))
		var dup *DuplicateKeyError
		if !errors.As(err, &dup) {
			t.Fatalf("input %s: want DuplicateKeyError, got %v", in, err)
		}
	}
}

func TestRejectNonCanonicalNumbers(t *testing.T) {
	bad := []string{
		`{"x":1.0}`, `{"x":1e3}`, `{"x":1E3}`, `{"x":01}`, `{"x":-0}`,
		`{"x":1.5}`, `{"x":-1.0}`, `{"x":[00]}`,
	}
	for _, in := range bad {
		if _, err := Parse([]byte(in)); err == nil {
			t.Fatalf("expected rejection of %s", in)
		}
	}
	good := []string{`{"x":0}`, `{"x":-1}`, `{"x":123456789012345678901234567890}`}
	for _, in := range good {
		if _, err := Parse([]byte(in)); err != nil {
			t.Fatalf("expected acceptance of %s: %v", in, err)
		}
	}
}

func TestRejectTrailingGarbageAndBOM(t *testing.T) {
	bad := []string{
		`{}extra`,
		`{} {}`,
		`true false`,
		`  `,
		"\xef\xbb\xbf{}",
		`{"x":"` + "\xff" + `"}`,
	}
	for _, in := range bad {
		if _, err := Parse([]byte(in)); err == nil {
			t.Fatalf("expected rejection of %q", in)
		}
	}
}

func TestStringEscaping(t *testing.T) {
	v, err := Parse([]byte(`{"s":"a/b < c > & d"}`))
	if err != nil {
		t.Fatal(err)
	}
	out, err := Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"s":"a/b < c > & d"}`
	if string(out) != want {
		t.Fatalf("html chars must not be escaped: got %q", out)
	}
}
