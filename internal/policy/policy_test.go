package policy

import "testing"

func TestLeafDecision(t *testing.T) {
	p := &Policy{
		Licenses: map[string]string{
			"MIT": "allow", "GPL-3.0-only": "deny", "GPL-2.0-only": "allow",
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
	cases := []struct {
		lic, exc, want string
	}{
		{"MIT", "", "allow"},
		{"GPL-3.0-only", "", "deny"},
		{"Unknown", "", "unknown"},
		// combo overrides
		{"GPL-2.0-only", "Classpath-exception-2.0", "allow"},
		{"GPL-3.0-only", "Classpath-exception-2.0", "deny"},
		// no combo: both pieces allow => allow
		{"MIT", "Classpath-exception-2.0", "allow"},
		// no combo: one piece denied => deny
		{"MIT", "GCC-exception-3.1", "deny"},
		{"GPL-3.0-only", "GCC-exception-3.1", "deny"},
		// no combo: unknown piece => unknown (never auto-allow)
		{"MIT", "Mystery-Exception", "unknown"},
		{"Mystery-License", "Classpath-exception-2.0", "unknown"},
	}
	for _, tc := range cases {
		if got := p.LeafDecision(tc.lic, tc.exc); got != tc.want {
			t.Errorf("LeafDecision(%q,%q) = %q, want %q", tc.lic, tc.exc, got, tc.want)
		}
	}
}

func TestValidate(t *testing.T) {
	bad := []Policy{
		{Licenses: map[string]string{"MIT": "maybe"}},
		{Combos: map[string]string{"NOPAIR": "allow"}},
		{Combos: map[string]string{"A WITH B WITH C": "allow"}},
	}
	for i, p := range bad {
		if err := p.Validate(); err == nil {
			t.Errorf("case %d: expected validation error", i)
		}
	}
	good := Policy{
		Licenses:   map[string]string{"MIT": "allow"},
		Exceptions: map[string]string{"E": "deny"},
		Combos:     map[string]string{"MIT WITH E": "deny"},
	}
	if err := good.Validate(); err != nil {
		t.Errorf("unexpected error: %v", err)
	}
}
