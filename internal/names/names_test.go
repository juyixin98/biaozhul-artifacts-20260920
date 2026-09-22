package names

import "testing"

func TestNormalize(t *testing.T) {
	cases := []struct {
		in      string
		want    string
		wantErr bool
	}{
		{"example.com", "example.com", false},
		{"EXAMPLE.COM", "example.com", false},                     // ASCII case folding
		{"Example.Com.", "example.com", false},                    // single trailing root dot
		{"  example.com  ", "example.com", false},                 // whitespace trimming
		{"bücher.example", "xn--bcher-kva.example", false},        // IDN -> A-label
		{"BÜCHER.EXAMPLE", "xn--bcher-kva.example", false},        // IDN case-insensitive
		{"xn--bcher-kva.example", "xn--bcher-kva.example", false}, // already A-label
		{"a.b", "a.b", false},
		{"example", "", true},       // need a TLD
		{"example..com", "", true},  // empty label
		{"example.com..", "", true}, // double trailing dot
		{"exa mple.com", "", true},  // interior whitespace
		{"", "", true},
	}
	for _, tc := range cases {
		got, err := Normalize(tc.in)
		if tc.wantErr {
			if err == nil {
				t.Errorf("Normalize(%q) = %q, want error", tc.in, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("Normalize(%q) unexpected error: %v", tc.in, err)
			continue
		}
		if got != tc.want {
			t.Errorf("Normalize(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestNormalizeEquivalence(t *testing.T) {
	// All of these must resolve to the same canonical name and therefore be
	// registrable only once.
	inputs := []string{"Shop.Example.COM", "shop.example.com.", "SHOP.EXAMPLE.COM"}
	want := "shop.example.com"
	for _, in := range inputs {
		got, err := Normalize(in)
		if err != nil {
			t.Fatalf("Normalize(%q): %v", in, err)
		}
		if got != want {
			t.Errorf("Normalize(%q) = %q, want %q", in, got, want)
		}
	}
}
