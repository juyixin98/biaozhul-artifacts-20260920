package semver

import "testing"

func TestParseAndCompare(t *testing.T) {
	cases := []struct {
		a, b string
		want int
	}{
		{"1.2.3", "1.2.3", 0},
		{"1.2.3", "1.2.4", -1},
		{"1.2.3", "1.3.0", -1},
		{"2.0.0", "1.9.9", 1},
		{"1.0.0-alpha", "1.0.0", -1},
		{"1.0.0-alpha", "1.0.0-alpha.1", -1},
		{"1.0.0-alpha.1", "1.0.0-alpha.beta", -1}, // 数字标识小于字母标识
		{"1.0.0-alpha.beta", "1.0.0-beta", -1},
		{"1.0.0-beta", "1.0.0-beta.2", -1},
		{"1.0.0-beta.2", "1.0.0-beta.11", -1}, // 数字按数值比较，不按字典序
		{"1.0.0-beta.11", "1.0.0-rc.1", -1},
		{"1.0.0-rc.1", "1.0.0", -1},
		{"1.0.0+build.1", "1.0.0+build.2", 0}, // 构建元数据不参与比较
		{"1.0.0-A", "1.0.0-a", -1},            // ASCII 字典序，大写字母码位更小
	}
	for _, tc := range cases {
		va, err := Parse(tc.a)
		if err != nil {
			t.Fatalf("parse %q: %v", tc.a, err)
		}
		vb, err := Parse(tc.b)
		if err != nil {
			t.Fatalf("parse %q: %v", tc.b, err)
		}
		if got := Compare(va, vb); got != tc.want {
			t.Errorf("Compare(%q,%q)=%d want %d", tc.a, tc.b, got, tc.want)
		}
	}
}

func TestParseInvalid(t *testing.T) {
	bad := []string{
		"", "1", "1.2", "1.2.3.4", "v1.2.3", "01.2.3", "1.02.3", "1.2.03",
		"1.2.3-", "1.2.3-alpha..1", "1.2.3-alpha.01", "1.2.3+", "1.2.3-+x",
		"latest", "1.2.x",
	}
	for _, s := range bad {
		if _, err := Parse(s); err == nil {
			t.Errorf("Parse(%q) expected error", s)
		}
	}
}
