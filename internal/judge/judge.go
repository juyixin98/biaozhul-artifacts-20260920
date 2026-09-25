// Package judge evaluates parsed SPDX expressions against a Policy.
//
// Decisions are fail-closed: anything the policy does not explicitly allow
// is never auto-approved.
//
//	AND  -> allow only when every branch allows; deny when any branch is
//	        denied; otherwise unknown.
//	OR   -> allow when at least one branch allows (the first such branch
//	        is reported as the chosen alternative); deny only when every
//	        branch is denied; otherwise unknown.
//
// This is the three-valued lattice deny < unknown < allow (AND = min,
// OR = max). Unknown means "not declared in policy, needs review" and is
// never treated as either an automatic approval or an automatic denial.
package judge

import (
	"fmt"

	"licensejudge/internal/policy"
	"licensejudge/internal/spdx"
)

// Decision is the top-level outcome.
type Decision string

const (
	Allow   Decision = "allow"
	Deny    Decision = "deny"
	Unknown Decision = "unknown"
)

// Reason explains why a sub-expression was not allowed.
type Reason struct {
	Code       string `json:"code"`
	Expression string `json:"expression"`
	Message    string `json:"message"`
}

// Alternative records one operand of an OR and its outcome. The evaluator
// keeps every branch so the caller can see both the rejected alternatives
// and (on success) which branch was selected.
type Alternative struct {
	Expression string   `json:"expression"`
	Decision   Decision `json:"decision"`
	Reasons    []Reason `json:"reasons,omitempty"`
}

// Result is the evaluation of a whole expression.
type Result struct {
	Expression   string        `json:"expression"`
	Decision     Decision      `json:"decision"`
	Selection    string        `json:"selection,omitempty"`
	Reasons      []Reason      `json:"reasons,omitempty"`
	Alternatives []Alternative `json:"alternatives,omitempty"`
}

// Evaluate parses (if needed) and judges an expression.
func Evaluate(expr string, pol *policy.Policy) (*Result, error) {
	node, err := spdx.Parse(expr)
	if err != nil {
		return nil, err
	}
	ev := evalNode(node, pol)
	if ev.decision == Allow {
		return &Result{
			Expression:   spdx.Print(node),
			Decision:     Allow,
			Selection:    ev.selection,
			Alternatives: ev.alternatives,
		}, nil
	}
	return &Result{
		Expression:   spdx.Print(node),
		Decision:     ev.decision,
		Reasons:      dedupeReasons(ev.reasons),
		Alternatives: ev.alternatives,
	}, nil
}

type eval struct {
	decision     Decision
	selection    string // canonical text of an allowed (sub)expression
	reasons      []Reason
	alternatives []Alternative
}

func evalNode(n spdx.Node, pol *policy.Policy) eval {
	switch v := n.(type) {
	case spdx.License:
		return evalLeaf(v, pol)
	case spdx.Binary:
		if v.Op == spdx.AND {
			return evalAnd(v, pol)
		}
		return evalOr(v, pol)
	default:
		// Cannot happen with the current parser.
		return eval{decision: Unknown, reasons: []Reason{{
			Code: "unsupported-node", Message: "unsupported expression node",
		}}}
	}
}

func evalLeaf(l spdx.License, pol *policy.Policy) eval {
	expr := l.String()
	switch pol.LeafDecision(l.ID, l.Exception) {
	case "allow":
		return eval{decision: Allow, selection: expr}
	case "deny":
		return eval{decision: Deny, reasons: []Reason{{
			Code: "denied", Expression: expr,
			Message: fmt.Sprintf("license %q is explicitly denied by policy", expr),
		}}}
	default:
		if l.Exception != "" {
			return eval{decision: Unknown, reasons: []Reason{{
				Code: "unknown-combination", Expression: expr,
				Message: fmt.Sprintf("combination %q is not declared in the policy; review required", expr),
			}}}
		}
		return eval{decision: Unknown, reasons: []Reason{{
			Code: "unknown-license", Expression: expr,
			Message: fmt.Sprintf("license %q is not declared in the policy; it is not auto-approved", expr),
		}}}
	}
}

func evalAnd(b spdx.Binary, pol *policy.Policy) eval {
	l := evalNode(b.Left, pol)
	r := evalNode(b.Right, pol)
	expr := spdx.Print(b)

	var reasons []Reason
	reasons = append(reasons, l.reasons...)
	reasons = append(reasons, r.reasons...)

	alts := mergeAlternatives(l.alternatives, r.alternatives)

	switch {
	case l.decision == Deny || r.decision == Deny:
		return eval{decision: Deny, reasons: withConjunction(expr, reasons, "AND requires every branch to be allowed"), alternatives: alts}
	case l.decision == Unknown || r.decision == Unknown:
		return eval{decision: Unknown, reasons: withConjunction(expr, reasons, "AND cannot be satisfied while a branch is unknown"), alternatives: alts}
	default:
		return eval{decision: Allow, selection: expr, alternatives: alts}
	}
}

func evalOr(b spdx.Binary, pol *policy.Policy) eval {
	expr := spdx.Print(b)

	// Flatten left-associative OR chains so each alternative is a leaf or
	// a group, e.g. "A OR B OR C" -> [A, B, C]. Each alternative records
	// its own decision (allow/deny/unknown), independent of siblings.
	var alts []Alternative
	for _, operand := range flattenOrOperands(b) {
		ev := evalNode(operand, pol)
		alts = append(alts, Alternative{
			Expression: spdx.Print(operand),
			Decision:   ev.decision,
			Reasons:    dedupeReasons(ev.reasons),
		})
	}

	for _, a := range alts {
		if a.Decision == Allow {
			return eval{decision: Allow, selection: a.Expression, alternatives: alts}
		}
	}

	var reasons []Reason
	for _, a := range alts {
		reasons = append(reasons, a.Reasons...)
	}

	hasDeny := false
	hasUnknown := false
	for _, a := range alts {
		if a.Decision == Deny {
			hasDeny = true
		}
		if a.Decision == Unknown {
			hasUnknown = true
		}
	}

	// Three-valued logic (deny < unknown < allow): OR is deny only when
	// every branch is deny; any unknown branch keeps it unknown so it is
	// reviewed rather than hard-rejected.
	var decision Decision
	var code, msg string
	switch {
	case hasUnknown:
		decision = Unknown
		code = "no-allowed-branch-unknown"
		msg = "OR has no allowed branch; at least one branch is unknown and needs policy review"
	case hasDeny:
		decision = Deny
		code = "all-branches-denied"
		msg = "OR has no allowed branch; every branch is explicitly denied"
	default:
		decision = Unknown
		code = "no-allowed-branch"
		msg = "OR has no allowed branch"
	}
	return eval{
		decision:     decision,
		reasons:      append(reasons, Reason{Code: code, Expression: expr, Message: msg}),
		alternatives: alts,
	}
}

// flattenOrOperands returns the leaf/group operands of a left-associative
// OR chain, in source order.
func flattenOrOperands(n spdx.Node) []spdx.Node {
	b, ok := n.(spdx.Binary)
	if !ok || b.Op != spdx.OR {
		return []spdx.Node{n}
	}
	return append(flattenOrOperands(b.Left), flattenOrOperands(b.Right)...)
}

func mergeAlternatives(a, b []Alternative) []Alternative {
	out := make([]Alternative, 0, len(a)+len(b))
	out = append(out, a...)
	out = append(out, b...)
	return out
}

func withConjunction(expr string, reasons []Reason, msg string) []Reason {
	return append(reasons, Reason{Code: "conjunction-unsatisfied", Expression: expr, Message: msg})
}

func dedupeReasons(in []Reason) []Reason {
	seen := map[string]bool{}
	out := make([]Reason, 0, len(in))
	for _, r := range in {
		key := r.Code + "|" + r.Expression
		if seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, r)
	}
	return out
}
