package judge

import (
	"testing"

	"licensejudge/internal/policy"
)

func testPolicy(t *testing.T) *policy.Policy {
	t.Helper()
	p := &policy.Policy{
		Name: "test",
		Licenses: map[string]string{
			"MIT": "allow", "Apache-2.0": "allow", "BSD-3-Clause": "allow",
			"GPL-3.0-only": "deny", "AGPL-3.0-only": "deny",
			"GPL-2.0-only": "allow",
		},
		Exceptions: map[string]string{
			"Classpath-exception-2.0": "allow",
			"GCC-exception-3.1":       "deny",
		},
		Combos: map[string]string{
			"GPL-2.0-only WITH Classpath-exception-2.0": "allow",
			"GPL-3.0-only WITH Classpath-exception-2.0": "deny",
		},
	}
	if err := p.Validate(); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestEvaluate(t *testing.T) {
	pol := testPolicy(t)
	cases := []struct {
		name      string
		expr      string
		want      Decision
		selection string // expected Selection on allow
	}{
		{"single allowed", "MIT", Allow, "MIT"},
		{"single denied", "GPL-3.0-only", Deny, ""},
		{"unknown license is not auto-approved", "WeirdProprietary-1.0", Unknown, ""},

		{"and both allowed", "MIT AND Apache-2.0", Allow, "MIT AND Apache-2.0"},
		{"and one denied", "MIT AND GPL-3.0-only", Deny, ""},
		{"and one unknown", "MIT AND WeirdThing", Unknown, ""},
		{"and denied plus unknown", "GPL-3.0-only AND WeirdThing", Deny, ""},

		{"or first branch allowed", "MIT OR GPL-3.0-only", Allow, "MIT"},
		{"or second branch allowed", "GPL-3.0-only OR MIT", Allow, "MIT"},
		{"or all denied", "GPL-3.0-only OR AGPL-3.0-only", Deny, ""},
		{"or unknown branch with denied branch", "WeirdThing OR GPL-3.0-only", Unknown, ""},
		{"or unknown branch with allowed branch", "WeirdThing OR MIT", Allow, "MIT"},

		{"precedence or-and chooses conjunction", "GPL-3.0-only OR MIT AND Apache-2.0", Allow, "MIT AND Apache-2.0"},
		{"parentheses force grouping", "(MIT OR GPL-3.0-only) AND Apache-2.0", Allow, "(MIT OR GPL-3.0-only) AND Apache-2.0"},
		{"parentheses all denied group", "(GPL-3.0-only OR AGPL-3.0-only) AND MIT", Deny, ""},

		{"with allowed combo", "GPL-2.0-only WITH Classpath-exception-2.0", Allow,
			"GPL-2.0-only WITH Classpath-exception-2.0"},
		{"with denied combo override", "GPL-3.0-only WITH Classpath-exception-2.0", Deny, ""},
		{"with denied exception", "MIT WITH GCC-exception-3.1", Deny, ""},
		{"with unknown exception", "MIT WITH Unknown-Exception", Unknown, ""},
		{"with in or picks allowed alternative",
			"GPL-3.0-only WITH Classpath-exception-2.0 OR MIT", Allow, "MIT"},

		{"chain three alternatives middle allowed", "GPL-3.0-only OR MIT OR WeirdThing", Allow, "MIT"},
		{"chain no allowed", "GPL-3.0-only OR WeirdThing OR AGPL-3.0-only", Unknown, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			res, err := Evaluate(tc.expr, pol)
			if err != nil {
				t.Fatalf("Evaluate(%q): %v", tc.expr, err)
			}
			if res.Decision != tc.want {
				t.Fatalf("decision = %q, want %q; reasons=%v", res.Decision, tc.want, res.Reasons)
			}
			if tc.want == Allow && res.Selection != tc.selection {
				t.Errorf("selection = %q, want %q", res.Selection, tc.selection)
			}
			if tc.want == Allow && len(res.Reasons) != 0 {
				t.Errorf("allowed result should carry no reasons, got %v", res.Reasons)
			}
			if tc.want != Allow && len(res.Reasons) == 0 {
				t.Errorf("non-allowed result must explain why, got no reasons")
			}
		})
	}
}

func TestOrAlternativesPreserved(t *testing.T) {
	pol := testPolicy(t)
	res, err := Evaluate("MIT OR GPL-3.0-only OR WeirdThing", pol)
	if err != nil {
		t.Fatal(err)
	}
	if res.Decision != Allow || res.Selection != "MIT" {
		t.Fatalf("got decision=%q selection=%q", res.Decision, res.Selection)
	}
	if len(res.Alternatives) != 3 {
		t.Fatalf("want 3 alternatives, got %d: %v", len(res.Alternatives), res.Alternatives)
	}
	want := []struct {
		expr string
		dec  Decision
	}{
		{"MIT", Allow},
		{"GPL-3.0-only", Deny},
		{"WeirdThing", Unknown},
	}
	for i, w := range want {
		got := res.Alternatives[i]
		if got.Expression != w.expr || got.Decision != w.dec {
			t.Errorf("alt[%d] = (%q,%q), want (%q,%q)", i, got.Expression, got.Decision, w.expr, w.dec)
		}
	}
}

func TestAllDeniedReportsAllBranchesDenied(t *testing.T) {
	pol := testPolicy(t)
	res, err := Evaluate("GPL-3.0-only OR AGPL-3.0-only", pol)
	if err != nil {
		t.Fatal(err)
	}
	if res.Decision != Deny {
		t.Fatalf("decision = %q, want deny when every branch is denied", res.Decision)
	}
	found := false
	for _, r := range res.Reasons {
		if r.Code == "all-branches-denied" {
			found = true
		}
	}
	if !found {
		t.Errorf("want all-branches-denied reason, got %v", res.Reasons)
	}
}

func TestInvalidExpression(t *testing.T) {
	pol := testPolicy(t)
	if _, err := Evaluate("MIT OR", pol); err == nil {
		t.Fatal("expected parse error")
	}
}
