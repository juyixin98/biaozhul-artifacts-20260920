package expression

import "testing"

func TestStringRoundTrip(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"MIT", "MIT"},
		{"MIT AND Apache-2.0", "MIT AND Apache-2.0"},
		{"MIT OR Apache-2.0 OR BSD-3-Clause", "MIT OR Apache-2.0 OR BSD-3-Clause"},
		// 优先级：AND 高于 OR，无需括号
		{"MIT OR Apache-2.0 AND GPL-3.0-only", "MIT OR Apache-2.0 AND GPL-3.0-only"},
		// 括号改变优先级时必须保留
		{"(MIT OR Apache-2.0) AND GPL-3.0-only", "(MIT OR Apache-2.0) AND GPL-3.0-only"},
		// 冗余括号在规范化后应最小化
		{"((MIT))", "MIT"},
		{"MIT AND (Apache-2.0 AND BSD-3-Clause)", "MIT AND Apache-2.0 AND BSD-3-Clause"},
		{"(MIT AND Apache-2.0) OR BSD-3-Clause", "MIT AND Apache-2.0 OR BSD-3-Clause"},
		// WITH 绑定
		{"LGPL-2.1-only WITH Classpath-exception-2.0", "LGPL-2.1-only WITH Classpath-exception-2.0"},
		{"MIT AND LGPL-2.1-only WITH Classpath-exception-2.0",
			"MIT AND LGPL-2.1-only WITH Classpath-exception-2.0"},
		// LicenseRef / DocumentRef
		{"LicenseRef-Internal-Eval", "LicenseRef-Internal-Eval"},
		{"DocumentRef-spdx:LicenseRef-Test", "DocumentRef-spdx:LicenseRef-Test"},
		// AND/OR 在 WITH 原子中不影响外层渲染
		{"(MIT OR BSD-3-Clause) AND LGPL-2.1-only WITH OCaml-LGPL-linking-exception",
			"(MIT OR BSD-3-Clause) AND LGPL-2.1-only WITH OCaml-LGPL-linking-exception"},
	}
	for _, c := range cases {
		t.Run(c.in, func(t *testing.T) {
			n, err := Parse(c.in)
			if err != nil {
				t.Fatalf("Parse(%q) 意外错误: %v", c.in, err)
			}
			if got := String(n); got != c.want {
				t.Errorf("String(%q) = %q, 期望 %q", c.in, got, c.want)
			}
		})
	}
}

func TestParsePrecedence(t *testing.T) {
	// A OR B AND C 应解析为 A OR (B AND C)
	n, err := Parse("MIT OR Apache-2.0 AND GPL-3.0-only")
	if err != nil {
		t.Fatal(err)
	}
	or, ok := n.(*OrNode)
	if !ok {
		t.Fatalf("期望顶层 OrNode, 实际 %T", n)
	}
	if _, ok := or.Right.(*AndNode); !ok {
		t.Fatalf("期望 OR 右侧为 AndNode（AND 优先级更高）, 实际 %T", or.Right)
	}
	if _, ok := or.Left.(*LicenseNode); !ok {
		t.Fatalf("期望 OR 左侧为 LicenseNode, 实际 %T", or.Left)
	}

	// (A OR B) AND C 应解析为 (A OR B) AND C
	n2, err := Parse("(MIT OR Apache-2.0) AND GPL-3.0-only")
	if err != nil {
		t.Fatal(err)
	}
	and, ok := n2.(*AndNode)
	if !ok {
		t.Fatalf("期望顶层 AndNode, 实际 %T", n2)
	}
	if _, ok := and.Left.(*OrNode); !ok {
		t.Fatalf("期望 AND 左侧为 OrNode, 实际 %T", and.Left)
	}
}

func TestLicensesOrderAndDedup(t *testing.T) {
	n, err := Parse("MIT AND MIT OR Apache-2.0 AND MIT")
	if err != nil {
		t.Fatal(err)
	}
	got := Licenses(n)
	want := []string{"MIT", "Apache-2.0"}
	if len(got) != len(want) {
		t.Fatalf("Licenses = %v, 期望 %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("Licenses = %v, 期望 %v", got, want)
		}
	}
}

func TestParseErrors(t *testing.T) {
	bad := []string{
		"",
		"   ",
		"MIT AND",
		"AND MIT",
		"MIT OR",
		"MIT WITH",
		"WITH Classpath-exception-2.0",
		"(MIT",
		"MIT)",
		"(MIT AND Apache-2.0",
		"MIT Apache-2.0",
		"MIT and Apache-2.0", // and 必须大写
		"MIT +",              // 不支持 + 后缀
		"GPL-2.0+",           // SPDX plus 形式不支持
		"(MIT OR Apache-2.0) WITH Classpath-exception-2.0", // WITH 不能作用于括号
		"MIT WITH (Classpath-exception-2.0)",
		"MIT AND OR Apache-2.0",
		"MIT @ Apache-2.0",
	}
	for _, in := range bad {
		t.Run(in, func(t *testing.T) {
			if _, err := Parse(in); err == nil {
				t.Errorf("Parse(%q) 期望错误，实际成功", in)
			}
		})
	}
}

func TestCaseSensitiveKeywords(t *testing.T) {
	// 小写 and 不是关键字，整体被当成单个标识符 -> 后续解析为"多余记号"错误。
	if _, err := Parse("MIT and Apache-2.0"); err == nil {
		t.Fatal("小写 and 不应被当作关键字")
	}
	// 但大小写混合的许可证 ID 作为标识符是合法的。
	if _, err := Parse("mIt"); err != nil {
		t.Fatalf("普通标识符 mIt 应解析成功: %v", err)
	}
}

func TestWithBindsAtom(t *testing.T) {
	// A AND B WITH E 中 WITH 绑定 B（atom 级），结构为 A AND (B WITH E)
	n, err := Parse("MIT AND LGPL-2.1-only WITH Classpath-exception-2.0")
	if err != nil {
		t.Fatal(err)
	}
	and, ok := n.(*AndNode)
	if !ok {
		t.Fatalf("期望 AndNode, 实际 %T", n)
	}
	if _, ok := and.Right.(*WithNode); !ok {
		t.Fatalf("期望 AND 右侧为 WithNode（WITH 绑定紧邻原子）, 实际 %T", and.Right)
	}
}
