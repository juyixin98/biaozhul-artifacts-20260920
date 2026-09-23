package main

import (
	"context"
	"encoding/json"
)

// JSON-RPC 2.0 标准错误码（见 https://www.jsonrpc.org/specification）。
const (
	codeParseError     = -32700
	codeInvalidRequest = -32600
	codeMethodNotFound = -32601
	codeInvalidParams  = -32602
	codeInternalError  = -32603
	// -32000 到 -32099 为保留的服务端实现错误区间。
	codeServerError = -32000
)

// RPCError 是 JSON-RPC error 对象。
type RPCError struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data,omitempty"`
}

func (e *RPCError) Error() string {
	return e.Message
}

// MethodFunc 是注册到网关上的方法实现。
// 返回 result（原始 JSON）或 *RPCError，二者互斥。
type MethodFunc func(ctx context.Context, params json.RawMessage) (json.RawMessage, *RPCError)

// Response 是 JSON-RPC 响应对象。
// result 为 *json.RawMessage 以便成功/失败时只出现 result 或 error 之一，
// 且允许 "result": null 被正确输出。
// ID 保留解析时的原始字节，因此字符串 "1" 与数字 1 不会被混淆。
type Response struct {
	JSONRPC string           `json:"jsonrpc"`
	Result  *json.RawMessage `json:"result,omitempty"`
	Error   *RPCError        `json:"error,omitempty"`
	ID      json.RawMessage  `json:"id"`
}

// newResponse 构造成功响应，id 为请求的原始 ID。
func newResponse(id json.RawMessage, result json.RawMessage) Response {
	r := result
	if r == nil {
		r = json.RawMessage("null")
	}
	return Response{JSONRPC: "2.0", Result: &r, ID: id}
}

// newErrorResponse 构造错误响应。无法确定 ID 时按规范使用 null。
func newErrorResponse(id json.RawMessage, code int, message string) Response {
	if id == nil {
		id = json.RawMessage("null")
	}
	return Response{
		JSONRPC: "2.0",
		Error:   &RPCError{Code: code, Message: message},
		ID:      id,
	}
}

// requestView 用于单项校验。使用原始消息保留 ID 与 params 的原始 JSON。
type requestView struct {
	JSONRPC string          `json:"jsonrpc"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
	ID      json.RawMessage `json:"id"`

	hasID    bool
	validID  bool
	isNotice bool
}

// parseItem 解析并校验单个请求项。
// 返回 (view, ok)：ok 为 false 表示该 JSON 值根本不是一个合法 Request 对象，
// 调用方应回复 -32600 Invalid Request（ID 尽量用 view.ID，无法确定则 null）。
func parseItem(raw json.RawMessage) (requestView, bool) {
	v := requestView{}

	// 先解进 map 以区分 "id 字段缺失"（通知）与 "id": null。
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return v, false
	}
	if len(fields) == 0 {
		// 合法 JSON 但不是对象（如 "x"、123、true、null、[]）。
		return v, false
	}

	if r, ok := fields["jsonrpc"]; ok {
		_ = json.Unmarshal(r, &v.JSONRPC)
	}
	if r, ok := fields["method"]; ok {
		_ = json.Unmarshal(r, &v.Method)
	}
	v.Params = fields["params"]

	if rawID, ok := fields["id"]; ok {
		v.hasID = true
		v.ID = rawID
		v.validID = isValidID(rawID)
	} else {
		v.isNotice = true
	}

	if v.JSONRPC != "2.0" || v.Method == "" {
		return v, false
	}
	if v.hasID && !v.validID {
		return v, false
	}
	if v.Params != nil && !isValidParams(v.Params) {
		return v, false
	}
	return v, true
}

// isValidID 按规范 ID 只能是字符串、数字（不含小数）或 null。
func isValidID(raw json.RawMessage) bool {
	trimmed := string(raw)
	if trimmed == "null" {
		return true
	}
	if len(trimmed) > 0 && trimmed[0] == '"' {
		return true
	}
	// 数字：交给 json.Number 解析，并拒绝小数 / 指数形式以外的写法。
	// json.Number 接受 1、1.0、1e3；规范建议使用整数，这里宽松接受任意合法 JSON 数字，
	// 但布尔、对象、数组一律拒绝。
	var n json.Number
	if err := json.Unmarshal(raw, &n); err == nil {
		return true
	}
	return false
}

// isValidParams 按规范 params 必须是数组或对象。
func isValidParams(raw json.RawMessage) bool {
	t := byte(0)
	for _, c := range raw {
		if c != ' ' && c != '\t' && c != '\n' && c != '\r' {
			t = c
			break
		}
	}
	return t == '[' || t == '{'
}
