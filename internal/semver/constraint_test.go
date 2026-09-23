package semver

import "testing"

func TestConstraintAllows(t *testing.T) {
	cases := []struct {
		expr string
		ok   map[string]bool // version -> expected Allows result
	}{
		{
			expr: "1.2.3",
			ok: map[string]bool{
				"1.2.3": true, "1.2.2": false, "1.2.4": false, "1.2.3-rc": false,
			},
		},
		{
			expr: ">=1.2.0 <2.0.0",
			ok: map[string]bool{
				"1.2.0": true, "1.9.9": true, "2.0.0": false, "1.1.9": false,
			},
		},
		{
			expr: "^1.2.3",
			ok: map[string]bool{
				"1.2.3": true, "1.9.0": true, "2.0.0": false, "1.2.2": false,
			},
		},
		{
			expr: "^0.2.3",
			ok: map[string]bool{
				"0.2.3": true, "0.2.9": true, "0.3.0": false,
			},
		},
		{
			expr: "^0.0.3",
			ok: map[string]bool{
				"0.0.3": true, "0.0.4": false, "0.0.3-1": false,
			},
		},
		{
			expr: "~1.2.3",
			ok: map[string]bool{
				"1.2.3": true, "1.2.9": true, "1.3.0": false, "2.0.0": false,
			},
		},
		{
			expr: "1.2.x",
			ok: map[string]bool{
				"1.2.0": true, "1.2.9": true, "1.3.0": false,
			},
		},
		{
			expr: "1.x",
			ok: map[string]bool{
				"1.0.0": true, "1.9.9": true, "2.0.0": false,
			},
		},
		{
			expr: "*",
			ok: map[string]bool{
				"0.0.0": true, "9.9.9": true, "1.0.0-alpha": false,
			},
		},
		{
			expr: ">=1.0.0 || >=2.0.0",
			ok: map[string]bool{
				"1.0.0": true, "0.9.0": false,
			},
		},
		{
			expr: ">1.0.0 <=2.0.0",
			ok: map[string]bool{
				"1.0.0": false, "1.5.0": true, "2.0.0": true, "2.0.1": false,
			},
		},
		{
			expr: "!=1.2.3",
			ok: map[string]bool{
				"1.2.3": false, "1.2.4": true,
			},
		},
	}
	for _, tc := range cases {
		c, err := ParseConstraint(tc.expr)
		if err != nil {
			t.Errorf("ParseConstraint(%q): %v", tc.expr, err)
			continue
		}
		for vs, want := range tc.ok {
			if got := c.Allows(MustParse(vs)); got != want {
				t.Errorf("%q.Allows(%s) = %v, want %v", tc.expr, vs, got, want)
			}
		}
	}
}

func TestPrereleaseGating(t *testing.T) {
	// A constraint without a prerelease must not admit prereleases.
	c := MustConstraint(">=1.0.0 <2.0.0")
	for _, v := range []string{"1.0.0-alpha", "1.5.0-beta.2", "2.0.0-alpha"} {
		if c.Allows(MustParse(v)) {
			t.Errorf("non-prerelease constraint admitted %s", v)
		}
	}
	// Mentioning the exact tuple admits prereleases of that tuple only.
	c = MustConstraint(">=1.0.0-alpha <2.0.0")
	if !c.Allows(MustParse("1.0.0-alpha")) {
		t.Error("1.0.0-alpha should be admitted by range mentioning 1.0.0-alpha")
	}
	if !c.Allows(MustParse("1.0.0-rc.9")) {
		t.Error("1.0.0-rc.9 shares tuple with 1.0.0-alpha")
	}
	if c.Allows(MustParse("1.5.0-beta")) {
		t.Error("1.5.0-beta tuple not mentioned, must be excluded")
	}
	if !c.Allows(MustParse("1.5.0")) {
		t.Error("releases within the range must remain admissible")
	}
}

func TestPrereleaseGatingExact(t *testing.T) {
	c := MustConstraint("1.0.0-rc.1")
	if !c.Allows(MustParse("1.0.0-rc.1")) {
		t.Error("exact prerelease should match")
	}
	if c.Allows(MustParse("1.0.0-rc.2")) {
		t.Error("different prerelease must not match exact constraint")
	}
}

func TestInvalidConstraint(t *testing.T) {
	for _, s := range []string{">>", "^x", "1.2.3-alpha..1 ||", ">=2.0.0 ||"} {
		if _, err := ParseConstraint(s); err == nil {
			t.Errorf("ParseConstraint(%q) expected error", s)
		}
	}
}
