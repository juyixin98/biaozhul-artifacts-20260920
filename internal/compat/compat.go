// Package compat 实现两个 schema 之间的兼容性判定。
//
// 方向语义（这是本项目的核心约定）：
//
//   - request（请求放宽方向）：服务端契约升级后，旧客户端发出的请求仍须被接受。
//     即 valid(old) ⊆ valid(new)，新请求 schema 只能“放宽”不能“收紧”。
//   - response（响应收紧方向）：新服务端产出的响应仍须落在旧消费者的处理能力内。
//     即 valid(new) ⊆ valid(old)，新响应 schema 只能“收紧”不能“放宽”。
//
// 两个方向统一归约为一次“生产者值集 ⊆ 消费者接受集”的子集检查：
// request 时生产者是 old、消费者是 new；response 时生产者是 new、消费者是 old。
//
// 任何一方出现不支持的 schema 关键字时，结论为 unknown 而不是通过。
package compat

import (
	"fmt"
	"time"

	"contractcheck/internal/schema"
)

// Direction 是兼容性检查方向。
type Direction string

const (
	// DirectionRequest 请求方向：新契约必须接受旧客户端的一切合法请求。
	DirectionRequest Direction = "request"
	// DirectionResponse 响应方向：新契约产出的响应必须被旧消费者接受。
	DirectionResponse Direction = "response"
)

// 结论状态。
const (
	StatusCompatible   = "compatible"
	StatusIncompatible = "incompatible"
	StatusUnknown      = "unknown"
)

// Finding 描述一处不兼容点，附触发不兼容的示例值。
type Finding struct {
	Path    string `json:"path"`    // JSON Pointer 风格路径，"" 表示根
	Kind    string `json:"kind"`    // type/enum/minimum/required/...
	Message string `json:"message"` // 人类可读说明
	// Example 是一个“生产者合法但消费者拒绝”的示例值（尽力生成）。
	Example any `json:"example,omitempty"`
}

// UnknownRef 记录某侧 schema 中出现的不支持关键字。
type UnknownRef struct {
	Side    string `json:"side"` // old 或 new
	Path    string `json:"path"`
	Keyword string `json:"keyword"`
}

// Report 是一次兼容性检查的结构化结果。
type Report struct {
	Direction Direction `json:"direction"`
	Status    string    `json:"status"`
	Findings  []Finding `json:"findings"`
	// UnknownKeywords 非空时 Status 必为 unknown。
	UnknownKeywords []UnknownRef `json:"unknownKeywords,omitempty"`
	CheckedAt       time.Time    `json:"checkedAt"`
}

// Check 比较 old 与 new 两个契约。now 由调用方（可控时钟）提供。
func Check(direction Direction, oldSchema, newSchema *schema.Schema, now time.Time) *Report {
	rep := &Report{
		Direction: direction,
		CheckedAt: now.UTC(),
	}

	for _, u := range oldSchema.Unknown {
		rep.UnknownKeywords = append(rep.UnknownKeywords, UnknownRef{Side: "old", Path: u.Path, Keyword: u.Keyword})
	}
	for _, u := range newSchema.Unknown {
		rep.UnknownKeywords = append(rep.UnknownKeywords, UnknownRef{Side: "new", Path: u.Path, Keyword: u.Keyword})
	}
	if len(rep.UnknownKeywords) > 0 {
		// 存在不理解的关键字：无法保证结论正确，返回未知而非通过。
		rep.Status = StatusUnknown
		rep.Findings = []Finding{}
		return rep
	}

	var producer, consumer *schema.Schema
	switch direction {
	case DirectionRequest:
		producer, consumer = oldSchema, newSchema
	case DirectionResponse:
		producer, consumer = newSchema, oldSchema
	default:
		rep.Status = StatusUnknown
		rep.Findings = []Finding{{
			Path:    "",
			Kind:    "direction",
			Message: fmt.Sprintf("未知的检查方向 %q，仅支持 request/response", direction),
		}}
		return rep
	}

	findings := []Finding{}
	checkSubset(producer, consumer, "", &findings)
	rep.Findings = findings
	if len(findings) == 0 {
		rep.Status = StatusCompatible
	} else {
		rep.Status = StatusIncompatible
	}
	return rep
}
