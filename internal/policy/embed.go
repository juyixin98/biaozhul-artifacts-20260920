// Package policy 持有冻结的准入策略：Rego 源码与基础镜像允许列表随二进制嵌入。
package policy

import (
	_ "embed"
	"encoding/json"
	"fmt"

	"mirrorsec/internal/model"
)

//go:embed policy.rego
var Source string

//go:embed allowlist.json
var allowlistJSON []byte

// Version 是策略冻结版本。任何策略修改都必须显式提升该版本。
const Version = "1.0.0-frozen"

// DefaultAllowlist 解析嵌入的基础镜像允许列表（仓库 + 精确摘要）。
func DefaultAllowlist() ([]model.AllowlistEntry, error) {
	var entries []model.AllowlistEntry
	if err := json.Unmarshal(allowlistJSON, &entries); err != nil {
		return nil, fmt.Errorf("解析嵌入的允许列表: %w", err)
	}
	if len(entries) == 0 {
		return nil, fmt.Errorf("嵌入的允许列表为空（拒绝启动）")
	}
	return entries, nil
}
