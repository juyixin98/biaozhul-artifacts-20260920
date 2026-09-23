package semver

import "testing"

func TestParseValid(t *testing.T) {
	cases := map[string]Version{
		"1.2.3":            {Major: 1, Minor: 2, Patch: 3},
		"v1.2.3":           {Major: 1, Minor: 2, Patch: 3},
		"0.0.0":            {},
		"1.2.3-alpha":      {Major: 1, Minor: 2, Patch: 3, Pre: []string{"alpha"}},
		"1.2.3-alpha.1":    {Major: 1, Minor: 2, Patch: 3, Pre: []string{"alpha", "1"}},
		"1.2.3+build.5":    {Major: 1, Minor: 2, Patch: 3, Build: "build.5"},
		"1.2.3-rc.1+build": {Major: 1, Minor: 2, Patch: 3, Pre: []string{"rc", "1"}, Build: "build"},
		"10.20.30":         {Major: 10, Minor: 20, Patch: 30},
	}
	for in, want := range cases {
		got, err := Parse(in)
		if err != nil {
			t.Errorf("Parse(%q) error: %v", in, err)
			continue
		}
		if got.Major != want.Major || got.Minor != want.Minor || got.Patch != want.Patch ||
			got.Build != want.Build || !equalStrings(got.Pre, want.Pre) {
			t.Errorf("Parse(%q) = %+v, want %+v", in, got, want)
		}
	}
}

func TestParseInvalid(t *testing.T) {
	for _, in := range []string{
		"", "1", "1.2", "1.2.3.4", "1.2.x", "1.2.-3", "1.2.3-", "1.2.3+",
		"1.2.3-alpha..1", "01.2.3", "1.2.3 ", "a.b.c",
	} {
		if _, err := Parse(in); err == nil {
			t.Errorf("Parse(%q) expected error", in)
		}
	}
}

func TestCompareOrdering(t *testing.T) {
	// SemVer 2.0.0 §11 example chain, extended.
	ordered := []string{
		"1.0.0-alpha",
		"1.0.0-alpha.1",
		"1.0.0-alpha.beta",
		"1.0.0-beta",
		"1.0.0-beta.2",
		"1.0.0-beta.11",
		"1.0.0-rc.1",
		"1.0.0",
		"1.0.1",
		"1.1.0",
		"2.0.0",
	}
	for i := 0; i < len(ordered); i++ {
		for j := 0; j < len(ordered); j++ {
			a, b := MustParse(ordered[i]), MustParse(ordered[j])
			want := 0
			if i < j {
				want = -1
			} else if i > j {
				want = 1
			}
			if got := Compare(a, b); got != want {
				t.Errorf("Compare(%s, %s) = %d, want %d", a, b, got, want)
			}
		}
	}
}

func TestCompareIgnoresBuild(t *testing.T) {
	if Compare(MustParse("1.0.0+a"), MustParse("1.0.0+b")) != 0 {
		t.Error("build metadata must not affect precedence")
	}
}

func TestStringRoundTrip(t *testing.T) {
	for _, s := range []string{"1.2.3", "0.1.0-alpha.2", "3.0.0+build"} {
		if got := MustParse(s).String(); got != s {
			t.Errorf("round trip: got %q, want %q", got, s)
		}
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
