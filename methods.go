package main

import (
	"context"
	"encoding/json"
	"fmt"
	"time"
)

// registerBuiltinMethods 注册示例方法，演示网关的各类行为。
func registerBuiltinMethods(g *Gateway) {
	// echo：原样返回 params，便于验证参数透传。
	g.Register("echo", func(_ context.Context, params json.RawMessage) (json.RawMessage, *RPCError) {
		if params == nil {
			return json.RawMessage("null"), nil
		}
		return params, nil
	})

	// add：对数组参数求和，如 [1, 2, 3] -> 6。
	g.Register("add", func(_ context.Context, params json.RawMessage) (json.RawMessage, *RPCError) {
		if params == nil {
			return nil, &RPCError{Code: codeInvalidParams, Message: "params required: array of numbers"}
		}
		var nums []float64
		if err := json.Unmarshal(params, &nums); err != nil {
			return nil, &RPCError{Code: codeInvalidParams, Message: "params must be an array of numbers"}
		}
		var sum float64
		for _, n := range nums {
			sum += n
		}
		out, _ := json.Marshal(sum)
		return out, nil
	})

	// fail：总是返回服务端错误，用于演示 error 响应。
	g.Register("fail", func(_ context.Context, _ json.RawMessage) (json.RawMessage, *RPCError) {
		return nil, &RPCError{Code: codeServerError, Message: "intentional failure"}
	})

	// slow：睡眠 params.ms 毫秒后返回 "ok"，用于演示并发与乱序完成。
	g.Register("slow", func(ctx context.Context, params json.RawMessage) (json.RawMessage, *RPCError) {
		ms := 100
		if params != nil {
			var p struct {
				MS int `json:"ms"`
			}
			if err := json.Unmarshal(params, &p); err != nil {
				return nil, &RPCError{Code: codeInvalidParams, Message: "params must be an object like {\"ms\": 100}"}
			}
			if p.MS < 0 || p.MS > 30000 {
				return nil, &RPCError{Code: codeInvalidParams, Message: "ms must be between 0 and 30000"}
			}
			ms = p.MS
		}
		select {
		case <-time.After(time.Duration(ms) * time.Millisecond):
			return json.RawMessage(fmt.Sprintf(`{"slept_ms":%d}`, ms)), nil
		case <-ctx.Done():
			return nil, &RPCError{Code: codeServerError, Message: "cancelled"}
		}
	})
}
