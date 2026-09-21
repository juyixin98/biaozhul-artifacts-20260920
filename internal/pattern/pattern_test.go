package pattern

import "testing"

func TestMatch(t *testing.T) {
	tests := []struct {
		pattern, name string
		want          bool
	}{
		{"chrome*", "Chrome 120", true},
		{"chrome*", "GoogleChrome", false},
		{"*game*", "Steam Game Launcher", true},
		{"*vault*", "HashiCorp Vault", true},
		{"code", "code", true},
		{"code", "Code", true}, // case-insensitive
		{"code?", "codes", true},
		{"code?", "code", false},
		{"1password*", "1Password 8", true},
		{"goland*", "GoLand", true},
		{"vs code*", "VS Code Insiders", true},
		{"zoom*", "Zoom Meetings", true},
		{"zoom*", "Mozilla Firefox", false},
		{"*secret-manager*", "Acme Secret-Manager", true},
	}
	for _, tc := range tests {
		if got := Match(tc.pattern, tc.name); got != tc.want {
			t.Errorf("Match(%q,%q) = %v, want %v", tc.pattern, tc.name, got, tc.want)
		}
	}
}

func TestValidate(t *testing.T) {
	if err := Validate("chrome*"); err != nil {
		t.Errorf("valid pattern rejected: %v", err)
	}
	if err := Validate(`bad\`); err == nil {
		t.Error("trailing backslash should be invalid")
	}
	if err := Validate(`ok\*literal`); err != nil {
		t.Errorf("escaped pattern rejected: %v", err)
	}
}
