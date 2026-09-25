// Package judge 在表达式 AST 之上执行三值策略判定：
//
//	allow   允许
//	deny    拒绝
//	unknown 未知许可证（既不自动通过也不自动拒绝）
//
// AND/OR 使用 Kleene 三值逻辑，WITH 组合在叶子处解析。
// OR 的每个备选分支都会被完整求值并保留在结果中，不会因为
// 短路而丢弃分支信息。
package judge

import (
	"fmt"

	"licensejudge/internal/expression"
	"licensejudge/internal/policy"
)

// Verdict 是判定结果三值。
type Verdict string

const (
	VerdictAllow   Verdict = "allow"
	VerdictDeny    Verdict = "deny"
	VerdictUnknown Verdict = "unknown"
)

// LeafFinding 是表达式中单个叶子（许可证或许可证 WITH 例外）的判定。
type LeafFinding struct {
	License   string   `json:"license"`
	Exception string   `json:"exception,omitempty"`
	Verdict   Verdict  `json:"verdict"`
	Reasons   []string `json:"reasons"`
}

// NodeResult 是任意子树的求值结果。
type NodeResult struct {
	Verdict Verdict       `json:"verdict"`
	Reasons []string      `json:"reasons"`
	Leaves  []LeafFinding `json:"leaves"`
}

// Alternative 是顶层 OR 的一个备选分支。
type Alternative struct {
	Expression string   `json:"expression"`
	Verdict    Verdict  `json:"verdict"`
	Reasons    []string `json:"reasons"`
}

// Result 是一次完整判定的输出。
type Result struct {
	Verdict      Verdict       `json:"verdict"`
	Selection    string        `json:"selection,omitempty"`
	Reasons      []string      `json:"reasons"`
	Alternatives []Alternative `json:"alternatives,omitempty"`
	Leaves       []LeafFinding `json:"leaves"`
	Disclaimer   string        `json:"disclaimer"`
}

// ParseError 包装表达式解析阶段的错误，便于上层区分错误码。
type ParseError struct{ Msg string }

func (e *ParseError) Error() string { return e.Msg }

func parseError(format string, args ...any) error {
	return &ParseError{Msg: fmt.Sprintf(format, args...)}
}

// Engine 绑定一份策略执行判定。
type Engine struct {
	policy *policy.Policy
}

// NewEngine 创建判定引擎。
func NewEngine(p *policy.Policy) *Engine {
	return &Engine{policy: p}
}

// EvalText 解析并判定表达式。空表达式返回错误。
func (e *Engine) EvalText(expr string) (*Result, error) {
	node, err := expression.Parse(expr)
	if err != nil {
		return nil, parseError("%s", err.Error())
	}
	return e.EvalAST(node), nil
}

// EvalAST 判定已解析的 AST。
func (e *Engine) EvalAST(root expression.Node) *Result {
	nr := e.eval(root)

	res := &Result{
		Verdict:    nr.Verdict,
		Reasons:    nonNil(nr.Reasons),
		Leaves:     nr.Leaves,
		Disclaimer: Disclaimer,
	}
	if res.Leaves == nil {
		res.Leaves = []LeafFinding{}
	}

	// 顶层 OR（含 A OR B OR C 左结合链）：展平为全部备选分支并保留，
	// 选择第一条可满足（allow）的分支。AND 下方嵌套的 OR 不在此列。
	if branches := flattenTopOr(root); len(branches) > 1 {
		res.Alternatives = make([]Alternative, 0, len(branches))
		for _, b := range branches {
			br := e.eval(b)
			res.Alternatives = append(res.Alternatives, Alternative{
				Expression: expression.String(b),
				Verdict:    br.Verdict,
				Reasons:    nonNil(br.Reasons),
			})
			if res.Selection == "" && br.Verdict == VerdictAllow {
				res.Selection = expression.String(b)
			}
		}
	}
	if res.Verdict == VerdictAllow && res.Selection == "" {
		// 顶层非 OR 的 allow：选择即规范化后的整条表达式。
		res.Selection = expression.String(root)
	}
	return res
}

// flattenTopOr 把顶层左结合的 OR 链展平为分支列表；非 OR 根返回单元素列表。
func flattenTopOr(root expression.Node) []expression.Node {
	var out []expression.Node
	var walk func(expression.Node)
	walk = func(n expression.Node) {
		if or, ok := n.(*expression.OrNode); ok {
			walk(or.Left)
			walk(or.Right)
			return
		}
		out = append(out, n)
	}
	walk(root)
	return out
}

// Disclaimer 是所有判定结果携带的非法律声明。
const Disclaimer = "本结果仅为基于所给配置的自动化合规判定，不构成法律意见；" +
	"许可证义务与例外的实际适用性需由具备资质的人员复核。"

func nonNil(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}
