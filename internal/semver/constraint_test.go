package semver

import "testing"

func TestConstraintSatisfy(t *testing.T) {
	// 每条用例：约束、版本、allowPrerelease、期望结果
	cases := []struct {
		con   string
		v     string
		allow bool
		want  bool
	}{
		// 精确与通配
		{"1.2.3", "1.2.3", false, true},
		{"1.2.3", "1.2.4", false, false},
		{"=1.2.3", "1.2.3", false, true},
		{"==1.2.3", "1.2.3", false, true},
		{"!=1.2.3", "1.2.4", false, true},
		{"!=1.2.3", "1.2.3", false, false},
		{"*", "9.9.9", false, true},
		{"x", "1.0.0", false, true},
		{"1", "1.9.9", false, true},
		{"1", "2.0.0", false, false},
		{"1.x", "1.0.0", false, true},
		{"1.2", "1.2.9", false, true},
		{"1.2", "1.3.0", false, false},
		{"1.2.*", "1.2.0", false, true},
		{"1.2.*", "1.3.0", false, false},
		// 比较符与部分版本
		{">=1.2.3", "1.2.3", false, true},
		{">1.2.3", "1.2.3", false, false},
		{"<2", "1.9.9", false, true},
		{"<2", "2.0.0", false, false},
		{"<2.1", "2.0.9", false, true},
		{"<2.1", "2.1.0", false, false},
		{">=1", "1.0.0", false, true},
		{">=1", "0.9.9", false, false},
		{"<=1.2", "1.2.9", false, true},
		{"<=1.2", "1.3.0", false, false},
		// caret
		{"^1.2.3", "1.9.0", false, true},
		{"^1.2.3", "2.0.0", false, false},
		{"^1.2.3", "1.2.2", false, false},
		{"^0.2.3", "0.2.9", false, true},
		{"^0.2.3", "0.3.0", false, false},
		{"^0.0.3", "0.0.3", false, true},
		{"^0.0.3", "0.0.4", false, false},
		{"^1", "1.9.9", false, true},
		{"^1.2", "1.2.9", false, true},
		{"^1.2", "1.3.0", false, true},
		{"^1.2", "2.0.0", false, false},
		// tilde
		{"~1.2.3", "1.2.9", false, true},
		{"~1.2.3", "1.3.0", false, false},
		{"~1.2", "1.2.9", false, true},
		{"~1", "1.9.9", false, true},
		{"~1", "2.0.0", false, false},
		// AND 组合（空格或逗号）
		{">=1.0.0 <2.0.0", "1.5.0", false, true},
		{">=1.0.0 <2.0.0", "2.0.0", false, false},
		{">=1.0.0,<2.0.0", "1.5.0", false, true},
		// OR
		{"1.x || 2.x", "2.5.0", false, true},
		{"1.x || 2.x", "3.0.0", false, false},
		// 预发布门控
		{"*", "1.0.0-rc.1", false, false},
		{"*", "1.0.0-rc.1", true, true},
		{">=1.0.0", "1.0.0-rc.1", false, false},
		{">=1.0.0-rc.1", "1.0.0-rc.1", false, true},
		{">=1.0.0-rc.1", "1.0.0-rc.2", false, true},
		{">=1.0.0-rc.1", "1.0.0", false, true},
		{">=1.0.0-rc.1 <2.0.0-beta.1", "2.0.0-beta.0", false, true},
		{">=1.0.0-rc.1 <2.0.0-beta.1", "2.0.0-beta.2", false, false},
		{"1.2.x", "1.2.0-alpha", false, false},
		{"^1.0.0-alpha", "1.0.0-beta", false, true},
	}
	for _, tc := range cases {
		c, err := ParseConstraint(tc.con)
		if err != nil {
			t.Fatalf("ParseConstraint(%q): %v", tc.con, err)
		}
		v, err := Parse(tc.v)
		if err != nil {
			t.Fatalf("Parse(%q): %v", tc.v, err)
		}
		if got := c.Satisfy(v, tc.allow); got != tc.want {
			t.Errorf("(%q).Satisfy(%q, allow=%v)=%v want %v", tc.con, tc.v, tc.allow, got, tc.want)
		}
	}
}

func TestParseConstraintInvalid(t *testing.T) {
	bad := []string{
		"", "   ", "1.2.3 ||", "|| 1.2.3", ">= 1.2.3", ">>1.2.3", "^*", "~x",
		"!=1.2", "==1", "1.2.3-alpha..1", "1.2.3.4", "1.x.2",
	}
	for _, s := range bad {
		if _, err := ParseConstraint(s); err == nil {
			t.Errorf("ParseConstraint(%q) expected error", s)
		}
	}
}

func TestMentionsPrerelease(t *testing.T) {
	c := MustParse(">=1.0.0-beta.1 <2.0.0")
	if !c.MentionsPrerelease([3]int64{1, 0, 0}) {
		t.Error("expected mention of 1.0.0")
	}
	if c.MentionsPrerelease([3]int64{1, 1, 0}) {
		t.Error("did not expect mention of 1.1.0")
	}
}
