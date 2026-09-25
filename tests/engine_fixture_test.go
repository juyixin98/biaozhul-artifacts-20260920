// Package tests 是由 testdata/fixtures 下显式夹具驱动的验收测试。
//
// 测试只执行夹具文件中列出的用例（表达式、期望裁决、HTTP 请求），
// 不内联额外的判定断言；新增验收场景应向夹具文件追加用例。
package tests

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"licensejudge/internal/judge"
	"licensejudge/internal/policy"
)

const fixtureDir = "../testdata/fixtures"

// EngineCase 是 engine_cases.json 的单条判定夹具。
type EngineCase struct {
	Name               string `json:"name"`
	Expression         string `json:"expression"`
	WantVerdict        string `json:"want_verdict,omitempty"`
	WantSelection      string `json:"want_selection,omitempty"`
	WantNoSelection    bool   `json:"want_no_selection,omitempty"`
	WantReasonContains string `json:"want_reason_contains,omitempty"`
	WantErrorContains  string `json:"want_error_contains,omitempty"`
}

// ParserCase 是 parser_cases.json 的单条解析错误夹具。
type ParserCase struct {
	Name              string `json:"name"`
	Expression        string `json:"expression"`
	WantErrorContains string `json:"want_error_contains"`
}

func loadFixtures(t *testing.T, name string, v any) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(fixtureDir, name))
	if err != nil {
		t.Fatalf("读取夹具 %s 失败: %v", name, err)
	}
	if err := json.Unmarshal(data, v); err != nil {
		t.Fatalf("解析夹具 %s 失败: %v", name, err)
	}
}

func loadExamplePolicy(t *testing.T) *policy.Policy {
	t.Helper()
	p, err := policy.LoadFile("../configs/policy.json")
	if err != nil {
		t.Fatalf("加载示例策略失败: %v", err)
	}
	return p
}

func TestEngineFixtures(t *testing.T) {
	var cases []EngineCase
	loadFixtures(t, "engine_cases.json", &cases)
	if len(cases) == 0 {
		t.Fatal("engine_cases.json 为空，夹具必须显式提供用例")
	}
	eng := judge.NewEngine(loadExamplePolicy(t))

	for _, c := range cases {
		t.Run(c.Name, func(t *testing.T) {
			res, err := eng.EvalText(c.Expression)
			if c.WantErrorContains != "" {
				if err == nil {
					t.Fatalf("期望错误包含 %q，实际判定成功: %s", c.WantErrorContains, res.Verdict)
				}
				if !strings.Contains(err.Error(), c.WantErrorContains) {
					t.Fatalf("错误 %q 不包含 %q", err.Error(), c.WantErrorContains)
				}
				return
			}
			if err != nil {
				t.Fatalf("表达式 %q 意外解析错误: %v", c.Expression, err)
			}
			if c.WantVerdict != "" && string(res.Verdict) != c.WantVerdict {
				t.Fatalf("verdict=%s, 期望 %s；原因=%v", res.Verdict, c.WantVerdict, res.Reasons)
			}
			if c.WantSelection != "" && res.Selection != c.WantSelection {
				t.Fatalf("selection=%q, 期望 %q", res.Selection, c.WantSelection)
			}
			if c.WantNoSelection && res.Selection != "" {
				t.Fatalf("期望无 selection，实际 %q", res.Selection)
			}
			if c.WantReasonContains != "" {
				joined := strings.Join(res.Reasons, " ")
				if !strings.Contains(joined, c.WantReasonContains) {
					t.Fatalf("理由 %v 不包含 %q", res.Reasons, c.WantReasonContains)
				}
			}
		})
	}
}

func TestParserFixtures(t *testing.T) {
	var cases []ParserCase
	loadFixtures(t, "parser_cases.json", &cases)
	if len(cases) == 0 {
		t.Fatal("parser_cases.json 为空，夹具必须显式提供用例")
	}
	eng := judge.NewEngine(loadExamplePolicy(t))

	for _, c := range cases {
		t.Run(c.Name, func(t *testing.T) {
			_, err := eng.EvalText(c.Expression)
			if err == nil {
				t.Fatalf("表达式 %q 期望解析错误，实际成功", c.Expression)
			}
			if c.WantErrorContains != "" && !strings.Contains(err.Error(), c.WantErrorContains) {
				t.Fatalf("错误 %q 不包含 %q", err.Error(), c.WantErrorContains)
			}
		})
	}
}
