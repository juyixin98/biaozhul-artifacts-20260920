package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"sync"
)

// maxBodyBytes 限制单个 HTTP 请求体大小，防止内存被打爆。
const maxBodyBytes = 4 << 20 // 4 MiB

// Gateway 是 JSON-RPC 2.0 网关：注册方法、处理单条/批量请求、限制并发。
type Gateway struct {
	mu      sync.RWMutex
	methods map[string]MethodFunc
	sem     chan struct{} // 全局并发上限信号量
}

// NewGateway 创建网关。maxConcurrency <= 0 时使用默认值 8。
func NewGateway(maxConcurrency int) *Gateway {
	if maxConcurrency <= 0 {
		maxConcurrency = 8
	}
	return &Gateway{
		methods: make(map[string]MethodFunc),
		sem:     make(chan struct{}, maxConcurrency),
	}
}

// Register 注册一个 JSON-RPC 方法。
func (g *Gateway) Register(name string, fn MethodFunc) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.methods[name] = fn
}

func (g *Gateway) lookup(name string) (MethodFunc, bool) {
	g.mu.RLock()
	defer g.mu.RUnlock()
	fn, ok := g.methods[name]
	return fn, ok
}

// ServeHTTP 实现 http.Handler。
func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, `{"error":"method not allowed, use POST"}`, http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBodyBytes))
	if err != nil {
		writeJSON(w, http.StatusRequestEntityTooLarge,
			newErrorResponse(nil, codeParseError, "failed to read request body"))
		return
	}

	// 1) 整体 JSON 语法校验。失败 → -32700 Parse error。
	if !json.Valid(body) {
		writeJSON(w, http.StatusOK,
			newErrorResponse(nil, codeParseError, "Parse error"))
		return
	}

	// 2) 区分单条与批量：看第一个非空白字符。
	if firstNonSpace(body) == '[' {
		g.serveBatch(w, r, body)
		return
	}
	g.serveSingle(w, r, body)
}

func firstNonSpace(b []byte) byte {
	for _, c := range b {
		if c != ' ' && c != '\t' && c != '\n' && c != '\r' {
			return c
		}
	}
	return 0
}

// serveSingle 处理单条请求。
func (g *Gateway) serveSingle(w http.ResponseWriter, r *http.Request, body json.RawMessage) {
	resp, isNotification := g.handleItem(r.Context(), body)
	if isNotification {
		// 通知不产生响应。
		w.WriteHeader(http.StatusNoContent)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// serveBatch 处理批量请求：并发执行，受全局信号量限制。
// 响应顺序不保证与输入一致（按完成顺序收集）。
func (g *Gateway) serveBatch(w http.ResponseWriter, r *http.Request, body json.RawMessage) {
	var items []json.RawMessage
	if err := json.Unmarshal(body, &items); err != nil {
		// 语法已校验过，理论上不会到这里；兜底按解析错误处理。
		writeJSON(w, http.StatusOK, newErrorResponse(nil, codeParseError, "Parse error"))
		return
	}
	// 空数组是 Invalid Request，按规范返回单个错误对象（非数组）。
	if len(items) == 0 {
		writeJSON(w, http.StatusOK,
			newErrorResponse(nil, codeInvalidRequest, "Invalid Request"))
		return
	}

	responses := make([]Response, 0, len(items))
	var mu sync.Mutex
	var wg sync.WaitGroup

	for _, item := range items {
		item := item
		wg.Add(1)
		go func() {
			defer wg.Done()
			resp, isNotification := g.handleItem(r.Context(), item)
			if isNotification {
				return
			}
			mu.Lock()
			responses = append(responses, resp)
			mu.Unlock()
		}()
	}
	wg.Wait()

	// 整批都是通知 → 不返回任何内容。
	if len(responses) == 0 {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	writeJSON(w, http.StatusOK, responses)
}

// handleItem 校验并执行单个请求项。
// 返回 (响应, 是否为通知)。通知即使出错也不产生响应。
func (g *Gateway) handleItem(ctx context.Context, raw json.RawMessage) (Response, bool) {
	v, ok := parseItem(raw)
	if !ok {
		// 非法项不是通知（通知必须是合法请求对象），一律回复 -32600。
		// 尽量带上能解析出的合法 ID，否则为 null。
		var id json.RawMessage
		if v.hasID && v.validID {
			id = v.ID
		}
		return newErrorResponse(id, codeInvalidRequest, "Invalid Request"), false
	}
	if v.isNotice {
		// 通知：照常执行（副作用可能发生），但绝不产生响应。
		g.dispatch(ctx, v)
		return Response{}, true
	}
	return g.dispatch(ctx, v), false
}

// dispatch 查找并调用方法，受并发上限约束。
func (g *Gateway) dispatch(ctx context.Context, v requestView) Response {
	fn, ok := g.lookup(v.Method)
	if !ok {
		return newErrorResponse(v.ID, codeMethodNotFound, "Method not found")
	}

	// 并发上限：获取信号量后才执行。
	select {
	case g.sem <- struct{}{}:
		defer func() { <-g.sem }()
	case <-ctx.Done():
		return newErrorResponse(v.ID, codeServerError, "request cancelled while waiting for worker")
	}

	result, rpcErr := fn(ctx, v.Params)
	if rpcErr != nil {
		return Response{JSONRPC: "2.0", Error: rpcErr, ID: idOrNull(v.ID)}
	}
	return newResponse(v.ID, result)
}

func idOrNull(id json.RawMessage) json.RawMessage {
	if id == nil {
		return json.RawMessage("null")
	}
	return id
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
