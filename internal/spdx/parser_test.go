package spdx

import "testing"

func TestParsePrecedence(t *testing.T) {
	// AND binds tighter than OR: "A OR B AND C" => A OR (B AND C)
	n, err := Parse("MIT OR Apache-2.0 AND BSD-3-Clause")
	if err != nil {
		t.Fatal(err)
	}
	b, ok := n.(Binary)
	if !ok || b.Op != OR {
		t.Fatalf("want top-level OR, got %#v", n)
	}
	if got := Print(b.Left); got != "MIT" {
		t.Errorf("left = %q, want MIT", got)
	}
	right, ok := b.Right.(Binary)
	if !ok || right.Op != AND {
		t.Fatalf("right side should be AND, got %#v", b.Right)
	}
	if got := Print(right); got != "Apache-2.0 AND BSD-3-Clause" {
		t.Errorf("right = %q", got)
	}
}

func TestParseParentheses(t *testing.T) {
	n, err := Parse("(MIT OR Apache-2.0) AND BSD-3-Clause")
	if err != nil {
		t.Fatal(err)
	}
	b, ok := n.(Binary)
	if !ok || b.Op != AND {
		t.Fatalf("want top-level AND, got %#v", n)
	}
	left, ok := b.Left.(Binary)
	if !ok || left.Op != OR {
		t.Fatalf("left side should be parenthesised OR, got %#v", b.Left)
	}
	if got := Print(left); got != "MIT OR Apache-2.0" {
		t.Errorf("group = %q", got)
	}
}

func TestParseNestedAndMixedCase(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"MIT and Apache-2.0", "MIT AND Apache-2.0"},
		{"(MIT)", "MIT"},
		{"((MIT OR Apache-2.0) AND (BSD-3-Clause OR ISC))",
			"(MIT OR Apache-2.0) AND (BSD-3-Clause OR ISC)"},
		{"MIT WITH Classpath-exception-2.0", "MIT WITH Classpath-exception-2.0"},
		{"(MIT OR Apache-2.0) WITH Classpath-exception-2.0", ""}, // WITH on group: error
		{"MIT OR", ""},         // missing operand
		{"AND MIT", ""},        // leading operator
		{"MIT(Apache", ""},     // malformed
		{"MIT Apache", ""},     // two idents
		{"(MIT OR Apache", ""}, // unclosed paren
		{"MIT WITH", ""},       // missing exception
		{"", ""},               // empty
		{"   ", ""},            // whitespace only
		{"MIT)", ""},           // unmatched close
	}
	for _, tc := range cases {
		n, err := Parse(tc.in)
		if tc.want == "" {
			if err == nil {
				t.Errorf("Parse(%q) = %v, want error", tc.in, Print(n))
			}
			continue
		}
		if err != nil {
			t.Errorf("Parse(%q) error: %v", tc.in, err)
			continue
		}
		if got := Print(n); got != tc.want {
			t.Errorf("Print(Parse(%q)) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestLeftAssociativeChain(t *testing.T) {
	n, err := Parse("MIT OR Apache-2.0 OR ISC")
	if err != nil {
		t.Fatal(err)
	}
	// ((MIT OR Apache-2.0) OR ISC)
	b, ok := n.(Binary)
	if !ok || b.Op != OR {
		t.Fatalf("want OR, got %#v", n)
	}
	if _, ok := b.Left.(Binary); !ok {
		t.Fatalf("left child should itself be an OR node, got %#v", b.Left)
	}
	if got := Print(b.Right); got != "ISC" {
		t.Errorf("rightmost = %q, want ISC", got)
	}
	if got := Print(n); got != "MIT OR Apache-2.0 OR ISC" {
		t.Errorf("print = %q", got)
	}
}

func TestWithPrecedence(t *testing.T) {
	// WITH binds tightest: MIT WITH E AND Apache => (MIT WITH E) AND Apache
	n, err := Parse("GPL-2.0-only WITH Classpath-exception-2.0 AND MIT")
	if err != nil {
		t.Fatal(err)
	}
	b, ok := n.(Binary)
	if !ok || b.Op != AND {
		t.Fatalf("want AND, got %#v", n)
	}
	lic, ok := b.Left.(License)
	if !ok || lic.Exception != "Classpath-exception-2.0" {
		t.Fatalf("left should be license with exception, got %#v", b.Left)
	}
}
