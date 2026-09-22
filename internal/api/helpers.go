package api

import "encoding/json"

// canonicalJSON 把请求里的元数据映射序列化为稳定 JSON（nil -> {}）。
func canonicalJSON(m map[string]any) []byte {
	if m == nil {
		return []byte("{}")
	}
	b, err := json.Marshal(m)
	if err != nil {
		return []byte("{}")
	}
	return b
}
