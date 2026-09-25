package judge

import (
	"strings"
	"testing"

	"licensejudge/internal/expression"
	"licensejudge/internal/policy"
)

func testPolicy(t *testing.T) *policy.Policy {
	t.Helper()
	p := &policy.Policy{
		Name:     "test",
		Unlisted: policy.DecisionUnknown,
		Licenses: map[string]policy.LicenseRule{
			"MIT":                 {Decision: policy.DecisionAllow},
			"Apache-2.0":          {Decision: policy.DecisionAllow},
			"BSD-3-Clause":        {Decision: policy.DecisionAllow},
			"ISC":                 {Decision: policy.DecisionAllow},
			"LGPL-2.1-only":       {Decision: policy.DecisionAllow},
			"MPL-2.0":             {Decision: policy.DecisionAllow},
			"GPL-2.0-only":        {Decision: policy.DecisionDeny},
			"GPL-3.0-only":        {Decision: policy.DecisionDeny},
			"AGPL-3.0-only":       {Decision: policy.DecisionDeny},
			"SSPL-1.0":            {Decision: policy.DecisionDeny},
			"LicenseRef-Pending":  {Decision: policy.DecisionUnknown},
			"LicenseRef-Internal": {Decision: policy.DecisionAllow},
			"LicenseRef-PropA":    {Decision: policy.DecisionDeny},
		},
		Exceptions: map[string]policy.ExceptionRule{
			"Classpath-exception-2.0":      {Allow: true, AppliesTo: []string{"LGPL-2.1-only", "GPL-2.0-only", "GPL-3.0-only"}},
			"OCaml-LGPL-linking-exception": {Allow: true, AppliesTo: []string{"LGPL-2.1-only"}},
			"Autoconf-exception-3.0":       {Allow: true, AppliesTo: []string{"GPL-3.0-only"}},
			"Bison-exception-2.2":          {Allow: false},
		},
	}
	if err := p.Validate(); err != nil {
		t.Fatalf("测试策略非法: %v", err)
	}
	return p
}

func eval(t *testing.T, p *policy.Policy, expr string) *Result {
	t.Helper()
	r, err := NewEngine(p).EvalText(expr)
	if err != nil {
		t.Fatalf("EvalText(%q) 意外错误: %v", expr, err)
	}
	return r
}

func TestSingleLicense(t *testing.T) {
	p := testPolicy(t)
	if eval(t, p, "MIT").Verdict != VerdictAllow {
		t.Error("MIT 应为 allow")
	}
	if eval(t, p, "GPL-3.0-only").Verdict != VerdictDeny {
		t.Error("GPL-3.0-only 应为 deny")
	}
	r := eval(t, p, "Totally-Unknown-1.0")
	if r.Verdict != VerdictUnknown {
		t.Error("未知许可证应为 unknown，不得自动通过")
	}
	if len(r.Reasons) == 0 || !strings.Contains(r.Reasons[0], "不自动通过") {
		t.Errorf("未知许可证理由应说明不自动通过，实际: %v", r.Reasons)
	}
}

func TestAndTruthTable(t *testing.T) {
	p := testPolicy(t)
	cases := map[string]Verdict{
		"MIT AND Apache-2.0":               VerdictAllow,
		"MIT AND GPL-3.0-only":             VerdictDeny,
		"GPL-2.0-only AND GPL-3.0-only":    VerdictDeny,
		"MIT AND Totally-Unknown":          VerdictUnknown,
		"GPL-3.0-only AND Totally-Unknown": VerdictDeny, // deny 优先
		"LicenseRef-Pending AND MIT":       VerdictUnknown,
	}
	for expr, want := range cases {
		if got := eval(t, p, expr).Verdict; got != want {
			t.Errorf("%s => %s, 期望 %s", expr, got, want)
		}
	}
}

func TestOrTruthTable(t *testing.T) {
	p := testPolicy(t)
	cases := map[string]Verdict{
		"MIT OR GPL-3.0-only":                    VerdictAllow,
		"GPL-2.0-only OR GPL-3.0-only":           VerdictDeny,
		"GPL-3.0-only OR Totally-Unknown":        VerdictUnknown, // 无 allow，有 unknown
		"MIT OR Totally-Unknown":                 VerdictAllow,
		"Totally-Unknown OR GPL-3.0-only":        VerdictUnknown,
		"LicenseRef-PropA OR LicenseRef-Pending": VerdictUnknown,
	}
	for expr, want := range cases {
		if got := eval(t, p, expr).Verdict; got != want {
			t.Errorf("%s => %s, 期望 %s", expr, got, want)
		}
	}
}

func TestOrBranchesPreserved(t *testing.T) {
	p := testPolicy(t)
	r := eval(t, p, "MIT OR Apache-2.0 OR GPL-3.0-only")
	if r.Verdict != VerdictAllow {
		t.Fatalf("verdict = %s", r.Verdict)
	}
	if r.Selection != "MIT" {
		t.Errorf("应选择第一条可满足分支 MIT，实际 selection=%q", r.Selection)
	}
	if len(r.Alternatives) != 3 {
		t.Fatalf("展平后的 alternatives 数量 = %d, 期望 3", len(r.Alternatives))
	}
	wantExprs := []string{"MIT", "Apache-2.0", "GPL-3.0-only"}
	for i, want := range wantExprs {
		if r.Alternatives[i].Expression != want {
			t.Errorf("alternatives[%d] = %q, 期望 %q", i, r.Alternatives[i].Expression, want)
		}
	}
	if r.Alternatives[2].Verdict != VerdictDeny {
		t.Errorf("GPL 分支应为 deny, 实际 %s", r.Alternatives[2].Verdict)
	}
}

func TestOrSelectionRightBranch(t *testing.T) {
	p := testPolicy(t)
	r := eval(t, p, "GPL-3.0-only OR MIT")
	if r.Selection != "MIT" {
		t.Errorf("左侧不满足时应选择右侧分支，selection=%q", r.Selection)
	}
}

func TestOrAllDenyNoSelection(t *testing.T) {
	p := testPolicy(t)
	r := eval(t, p, "GPL-2.0-only OR GPL-3.0-only")
	if r.Selection != "" {
		t.Errorf("全部拒绝时不应有 selection，实际 %q", r.Selection)
	}
	if !strings.Contains(strings.Join(r.Reasons, " "), "均被拒绝") {
		t.Errorf("拒绝理由应说明全部备选被拒绝: %v", r.Reasons)
	}
}

func TestPrecedenceChangesResult(t *testing.T) {
	p := testPolicy(t)
	// (MIT OR GPL-3.0-only) AND GPL-2.0-only => deny（括号：左子树 allow，与 GPL2 相遇 deny）
	r1 := eval(t, p, "(MIT OR GPL-3.0-only) AND GPL-2.0-only")
	if r1.Verdict != VerdictDeny {
		t.Errorf("括号版本应 deny, 实际 %s", r1.Verdict)
	}
	// MIT OR GPL-3.0-only AND GPL-2.0-only => MIT allow 使整体 allow（AND 优先）
	r2 := eval(t, p, "MIT OR GPL-3.0-only AND GPL-2.0-only")
	if r2.Verdict != VerdictAllow {
		t.Errorf("无括号版本应 allow（MIT 分支）, 实际 %s", r2.Verdict)
	}
	if r2.Selection != "MIT" {
		t.Errorf("无括号版本应选择 MIT, 实际 %q", r2.Selection)
	}
}

func TestWithCombinations(t *testing.T) {
	p := testPolicy(t)
	cases := []struct {
		expr string
		want Verdict
	}{
		{"LGPL-2.1-only WITH Classpath-exception-2.0", VerdictAllow},
		{"LGPL-2.1-only WITH OCaml-LGPL-linking-exception", VerdictAllow},
		{"MIT WITH Classpath-exception-2.0", VerdictDeny}, // 例外不绑定 MIT
		{"MIT WITH Totally-Unknown-Exception", VerdictUnknown},
		{"LGPL-2.1-only WITH Bison-exception-2.2", VerdictDeny},    // 拒绝型例外
		{"GPL-3.0-only WITH Classpath-exception-2.0", VerdictDeny}, // 被拒许可证不能靠例外翻盘
		{"GPL-3.0-only WITH Autoconf-exception-3.0", VerdictDeny},
		{"Totally-Unknown WITH Classpath-exception-2.0", VerdictUnknown},
		{"Totally-Unknown WITH Totally-Unknown-Exception", VerdictUnknown},
	}
	for _, c := range cases {
		if got := eval(t, p, c.expr).Verdict; got != c.want {
			t.Errorf("%s => %s, 期望 %s", c.expr, got, c.want)
		}
	}
}

func TestWithDenyReason(t *testing.T) {
	p := testPolicy(t)
	r := eval(t, p, "MIT WITH Classpath-exception-2.0")
	joined := strings.Join(r.Reasons, " ")
	if !strings.Contains(joined, "绑定") {
		t.Errorf("应说明绑定约束不满足，实际: %v", r.Reasons)
	}
}

func TestUnknownDoesNotAutoPassEvenWithAllowedException(t *testing.T) {
	p := testPolicy(t)
	// Classpath 例外绑定清单含未知许可证不可能；构造一个无绑定限制的允许例外。
	p.Exceptions["Free-For-All-Exception"] = policy.ExceptionRule{Allow: true}
	if err := p.Validate(); err != nil {
		t.Fatal(err)
	}
	r := eval(t, p, "Never-Seen-License WITH Free-For-All-Exception")
	if r.Verdict != VerdictUnknown {
		t.Fatalf("未知许可证即使配允许例外也必须 unknown，实际 %s", r.Verdict)
	}
}

func TestComplexExpression(t *testing.T) {
	p := testPolicy(t)
	expr := "(MIT OR GPL-3.0-only) AND (Apache-2.0 OR LGPL-2.1-only WITH Classpath-exception-2.0)"
	r := eval(t, p, expr)
	if r.Verdict != VerdictAllow {
		t.Fatalf("复合表达式应 allow，实际 %s: %v", r.Verdict, r.Reasons)
	}
	if len(r.Leaves) != 4 {
		t.Errorf("叶子数 = %d, 期望 4", len(r.Leaves))
	}
}

func TestDisclaimerAttached(t *testing.T) {
	p := testPolicy(t)
	r := eval(t, p, "MIT")
	if !strings.Contains(r.Disclaimer, "不构成法律") {
		t.Errorf("结果必须携带非法律声明: %q", r.Disclaimer)
	}
}

func TestEvalASTAndEvalTextConsistency(t *testing.T) {
	p := testPolicy(t)
	n, err := expression.Parse("MIT AND Apache-2.0")
	if err != nil {
		t.Fatal(err)
	}
	r1 := NewEngine(p).EvalAST(n)
	r2 := eval(t, p, "MIT AND Apache-2.0")
	if r1.Verdict != r2.Verdict {
		t.Error("EvalAST 与 EvalText 结果不一致")
	}
}

func TestParseErrorType(t *testing.T) {
	p := testPolicy(t)
	_, err := NewEngine(p).EvalText("MIT AND")
	if err == nil {
		t.Fatal("期望解析错误")
	}
	if _, ok := err.(*ParseError); !ok {
		t.Fatalf("错误类型应为 *ParseError, 实际 %T", err)
	}
}

func TestNonNilSlices(t *testing.T) {
	p := testPolicy(t)
	r := eval(t, p, "MIT OR GPL-3.0-only")
	if r.Reasons == nil || r.Leaves == nil {
		t.Fatal("Reasons/Leaves 不应为 nil")
	}
	for _, a := range r.Alternatives {
		if a.Reasons == nil {
			t.Fatal("alternative reasons 不应为 nil")
		}
	}
}
