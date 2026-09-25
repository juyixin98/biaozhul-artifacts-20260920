// Package policy 定义自定义许可证策略：已知许可证的允许/拒绝裁决、
// 例外（WITH）规则，以及"未列出许可证"的处置方式。
//
// 设计原则：策略只能把未列出许可证配置为 unknown（默认）。
// 配置为 allow 会在加载阶段被拒绝，避免"未知即放行"。
package policy

import "fmt"

// Decision 是策略中的静态裁决值。
type Decision string

const (
	DecisionAllow   Decision = "allow"
	DecisionDeny    Decision = "deny"
	DecisionUnknown Decision = "unknown"
)

// LicenseRule 是单个许可证的规则。
type LicenseRule struct {
	// Decision 为 allow/deny/unknown，必填。
	Decision Decision `json:"decision"`
	// Reason 给出 allow/deny 的人为理由（用于审计展示）。
	Reason string `json:"reason,omitempty"`
}

// ExceptionRule 描述 "License WITH Exception" 组合的规则。
type ExceptionRule struct {
	// Allow 为 true 表示该组合允许；false 表示拒绝。
	Allow bool `json:"allow"`
	// AppliesTo 声明该例外允许绑定的许可证 ID 清单（绑定约束）。
	// 为空表示不限制绑定的许可证。仅对 allow 规则生效；
	// WITH 组合的许可证不在清单内时，组合被拒绝。
	AppliesTo []string `json:"applies_to,omitempty"`
	// Reason 是审计理由。
	Reason string `json:"reason,omitempty"`
}

// Policy 是一份完整的许可策略。
type Policy struct {
	Name       string                   `json:"name"`
	Version    string                   `json:"version,omitempty"`
	Licenses   map[string]LicenseRule   `json:"licenses"`
	Exceptions map[string]ExceptionRule `json:"exceptions,omitempty"`
	// Unlisted 只允许取值 "unknown" 或缺省（按 unknown 处理）。
	Unlisted Decision `json:"unlisted,omitempty"`
}

// LookupLicense 返回许可证规则及是否已列出。
func (p *Policy) LookupLicense(id string) (LicenseRule, bool) {
	if r, ok := p.Licenses[id]; ok {
		return r, true
	}
	return LicenseRule{}, false
}

// LookupException 返回例外规则及是否已配置。
func (p *Policy) LookupException(id string) (ExceptionRule, bool) {
	if p.Exceptions == nil {
		return ExceptionRule{}, false
	}
	r, ok := p.Exceptions[id]
	return r, ok
}

// LicenseDecision 返回策略对某许可证的静态裁决。
// 未列出的许可证始终返回 unknown（加载校验保证 Unlisted 不会是 allow/deny）。
func (p *Policy) LicenseDecision(id string) Decision {
	r, ok := p.LookupLicense(id)
	if !ok {
		return DecisionUnknown
	}
	return r.Decision
}

// Validate 检查策略完整性。
func (p *Policy) Validate() error {
	if p.Name == "" {
		return fmt.Errorf("策略缺少 name 字段")
	}
	if p.Licenses == nil {
		p.Licenses = map[string]LicenseRule{}
	}
	for id, r := range p.Licenses {
		if id == "" {
			return fmt.Errorf("许可证 ID 不能为空")
		}
		switch r.Decision {
		case DecisionAllow, DecisionDeny, DecisionUnknown:
		default:
			return fmt.Errorf("许可证 %q 的 decision=%q 非法，仅允许 allow/deny/unknown", id, r.Decision)
		}
	}
	for name, ex := range p.Exceptions {
		if name == "" {
			return fmt.Errorf("例外 ID 不能为空")
		}
		if !ex.Allow && len(ex.AppliesTo) > 0 {
			return fmt.Errorf("例外 %q 为拒绝规则时不能声明 applies_to", name)
		}
	}
	switch p.Unlisted {
	case "", DecisionUnknown:
		p.Unlisted = DecisionUnknown
	case DecisionAllow:
		return fmt.Errorf("策略 %q 不允许把 unlisted 配置为 allow：未知许可证必须返回 unknown", p.Name)
	case DecisionDeny:
		return fmt.Errorf("策略 %q 不允许把 unlisted 配置为 deny：未知许可证必须返回 unknown（在 licenses 中显式拒绝具体许可证）", p.Name)
	default:
		return fmt.Errorf("unlisted=%q 非法，仅允许 unknown", p.Unlisted)
	}
	return nil
}
