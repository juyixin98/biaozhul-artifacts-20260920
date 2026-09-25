package judge

import (
	"fmt"

	"licensejudge/internal/expression"
	"licensejudge/internal/policy"
)

// eval 对任意子树求值。叶子信息按从左到右顺序保留，不做短路。
func (e *Engine) eval(n expression.Node) *NodeResult {
	switch v := n.(type) {
	case *expression.LicenseNode:
		return e.evalLicense(v.License, "")
	case *expression.WithNode:
		return e.evalWith(v)
	case *expression.AndNode:
		l := e.eval(v.Left)
		r := e.eval(v.Right)
		return combineAnd(l, r)
	case *expression.OrNode:
		l := e.eval(v.Left)
		r := e.eval(v.Right)
		return combineOr(l, r)
	default:
		return &NodeResult{Verdict: VerdictUnknown, Reasons: []string{}, Leaves: []LeafFinding{}}
	}
}

// evalLicense 判定单个许可证叶子。exception 为空表示无 WITH 子句。
func (e *Engine) evalLicense(id, exception string) *NodeResult {
	leaf := LeafFinding{License: id, Exception: exception, Reasons: []string{}}
	switch e.policy.LicenseDecision(id) {
	case policy.DecisionAllow:
		leaf.Verdict = VerdictAllow
		leaf.Reasons = append(leaf.Reasons, fmt.Sprintf("许可证 %s 被策略允许", id))
	case policy.DecisionDeny:
		leaf.Verdict = VerdictDeny
		leaf.Reasons = append(leaf.Reasons, fmt.Sprintf("许可证 %s 被策略拒绝", id))
	case policy.DecisionUnknown:
		leaf.Verdict = VerdictUnknown
		leaf.Reasons = append(leaf.Reasons, fmt.Sprintf("许可证 %s 未在策略中列出，裁决为未知（不自动通过）", id))
	}
	return &NodeResult{Verdict: leaf.Verdict, Reasons: append([]string{}, leaf.Reasons...), Leaves: []LeafFinding{leaf}}
}

// evalWith 判定 "License WITH Exception" 叶子。
//
// 判定次序：
//  1. 许可证被策略显式拒绝 → deny，例外不能翻盘；
//  2. 许可证未知 → unknown（无论例外是否已知或许可如何绑定，
//     对未知许可证无法得出绑定结论，绝不自动通过）；
//  3. 许可证已知：例外未知 → unknown；例外已知且违反绑定约束 → deny；
//     再按例外规则的 allow/deny 裁决。
func (e *Engine) evalWith(v *expression.WithNode) *NodeResult {
	id := v.License.License
	ex := v.Exception
	leaf := LeafFinding{License: id, Exception: ex, Reasons: []string{}}

	licDecision := e.policy.LicenseDecision(id)
	exRule, exKnown := e.policy.LookupException(ex)

	var reasons []string
	var verdict Verdict

	switch {
	case licDecision == policy.DecisionDeny:
		// 策略中被显式拒绝的许可证，不能通过例外翻盘。
		verdict = VerdictDeny
		reasons = append(reasons, fmt.Sprintf("许可证 %s 被策略拒绝，例外 %s 不能改变拒绝结论", id, ex))
	case licDecision == policy.DecisionUnknown:
		// 未知许可证：例外无法消除许可证本身的未知性。
		verdict = VerdictUnknown
		reasons = append(reasons, fmt.Sprintf("许可证 %s 未在策略中列出，裁决为未知（不自动通过）", id))
		if exKnown {
			reasons = append(reasons, fmt.Sprintf(
				"即使例外 %s 已配置，也不能据此对未知许可证 %s 作出通过结论", ex, id))
		} else {
			reasons = append(reasons, fmt.Sprintf("例外 %s 未在策略中配置", ex))
		}
	case !exKnown:
		// 已知许可证 + 未知例外：未知（不能假定例外成立）。
		verdict = VerdictUnknown
		reasons = append(reasons, fmt.Sprintf("例外 %s 未在策略中配置，无法确认 %s WITH %s 的组合", ex, id, ex))
	default:
		// 许可证已知、例外已知：检查例外的许可证绑定约束。
		if len(exRule.AppliesTo) > 0 && !containsString(exRule.AppliesTo, id) {
			verdict = VerdictDeny
			reasons = append(reasons, fmt.Sprintf(
				"例外 %s 仅允许与 %v 绑定，许可证 %s 不在允许清单内，组合拒绝", ex, exRule.AppliesTo, id))
		} else if exRule.Allow {
			verdict = VerdictAllow
			reasons = append(reasons, fmt.Sprintf("组合 %s WITH %s 被策略允许", id, ex))
		} else {
			verdict = VerdictDeny
			reasons = append(reasons, fmt.Sprintf("组合 %s WITH %s 被策略拒绝", id, ex))
		}
	}

	leaf.Verdict = verdict
	leaf.Reasons = reasons
	return &NodeResult{Verdict: verdict, Reasons: append([]string{}, reasons...), Leaves: []LeafFinding{leaf}}
}

func containsString(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// combineAnd 按 Kleene 三值逻辑合并 AND：
//
//	deny 优先：任一 deny => deny
//	其次 unknown：任一 unknown => unknown
//	否则 allow
func combineAnd(l, r *NodeResult) *NodeResult {
	nr := &NodeResult{Leaves: append(append([]LeafFinding{}, l.Leaves...), r.Leaves...)}
	var verdicts []Verdict
	for _, x := range []*NodeResult{l, r} {
		verdicts = append(verdicts, x.Verdict)
	}
	switch {
	case contains(verdicts, VerdictDeny):
		nr.Verdict = VerdictDeny
		nr.Reasons = andDenyReasons(l, r)
	case contains(verdicts, VerdictUnknown):
		nr.Verdict = VerdictUnknown
		nr.Reasons = andUnknownReasons(l, r)
	default:
		nr.Verdict = VerdictAllow
		nr.Reasons = []string{"AND 两侧均被允许，组合允许"}
	}
	nr.Reasons = dedup(nr.Reasons)
	return nr
}

// combineOr 按 Kleene 三值逻辑合并 OR：
//
//	allow 优先：任一 allow => allow
//	其次 unknown：任一 unknown => unknown
//	否则 deny
func combineOr(l, r *NodeResult) *NodeResult {
	nr := &NodeResult{Leaves: append(append([]LeafFinding{}, l.Leaves...), r.Leaves...)}
	var verdicts []Verdict
	for _, x := range []*NodeResult{l, r} {
		verdicts = append(verdicts, x.Verdict)
	}
	switch {
	case contains(verdicts, VerdictAllow):
		nr.Verdict = VerdictAllow
		nr.Reasons = orAllowReasons(l, r)
	case contains(verdicts, VerdictUnknown):
		nr.Verdict = VerdictUnknown
		nr.Reasons = orUnknownReasons(l, r)
	default:
		nr.Verdict = VerdictDeny
		nr.Reasons = []string{"OR 的所有备选分支均被拒绝"}
	}
	nr.Reasons = dedup(nr.Reasons)
	return nr
}

func andDenyReasons(l, r *NodeResult) []string {
	var out []string
	switch {
	case l.Verdict == VerdictDeny && r.Verdict == VerdictDeny:
		out = append(out, "AND 两侧均被拒绝，组合拒绝")
	case l.Verdict == VerdictDeny:
		out = append(out, "AND 左侧被拒绝，整个组合拒绝")
	default:
		out = append(out, "AND 右侧被拒绝，整个组合拒绝")
	}
	out = append(out, decisiveReasons(l, VerdictDeny)...)
	out = append(out, decisiveReasons(r, VerdictDeny)...)
	return out
}

func andUnknownReasons(l, r *NodeResult) []string {
	var out []string
	switch {
	case l.Verdict == VerdictUnknown && r.Verdict == VerdictUnknown:
		out = append(out, "AND 两侧均含未知项，无法确认通过")
	case l.Verdict == VerdictUnknown:
		if r.Verdict == VerdictAllow {
			out = append(out, "AND 左侧含未知项，即使右侧允许，组合仍裁决未知")
		}
	default:
		if l.Verdict == VerdictAllow {
			out = append(out, "AND 右侧含未知项，即使左侧允许，组合仍裁决未知")
		}
	}
	out = append(out, decisiveReasons(l, VerdictUnknown)...)
	out = append(out, decisiveReasons(r, VerdictUnknown)...)
	return out
}

func orAllowReasons(l, r *NodeResult) []string {
	var out []string
	switch {
	case l.Verdict == VerdictAllow && r.Verdict == VerdictAllow:
		out = append(out, "OR 两侧均允许，至少一条分支满足策略")
	case l.Verdict == VerdictAllow:
		out = append(out, "OR 左侧允许，选择左侧分支即可满足策略")
	default:
		out = append(out, "OR 右侧允许，选择右侧分支即可满足策略")
	}
	out = append(out, decisiveReasons(l, VerdictAllow)...)
	out = append(out, decisiveReasons(r, VerdictAllow)...)
	return out
}

func orUnknownReasons(l, r *NodeResult) []string {
	var out []string
	switch {
	case l.Verdict == VerdictUnknown && r.Verdict == VerdictUnknown:
		out = append(out, "OR 两侧均含未知项且无允许分支，裁决未知")
	case l.Verdict == VerdictUnknown:
		out = append(out, "OR 无允许分支；左侧含未知项，不能据此通过或拒绝，裁决未知")
	default:
		out = append(out, "OR 无允许分支；右侧含未知项，不能据此通过或拒绝，裁决未知")
	}
	out = append(out, decisiveReasons(l, VerdictUnknown)...)
	out = append(out, decisiveReasons(r, VerdictUnknown)...)
	out = append(out, decisiveReasons(l, VerdictDeny)...)
	out = append(out, decisiveReasons(r, VerdictDeny)...)
	return out
}

// decisiveReasons 返回某子树中与给定裁决一致的叶子理由。
func decisiveReasons(nr *NodeResult, v Verdict) []string {
	var out []string
	for _, leaf := range nr.Leaves {
		if leaf.Verdict == v {
			out = append(out, leaf.Reasons...)
		}
	}
	return out
}

func contains(vs []Verdict, target Verdict) bool {
	for _, v := range vs {
		if v == target {
			return true
		}
	}
	return false
}

func dedup(in []string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(in))
	for _, s := range in {
		if s == "" || seen[s] {
			continue
		}
		seen[s] = true
		out = append(out, s)
	}
	return out
}
