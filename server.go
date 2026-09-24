package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"sync"
)

// Handler is a registered JSON-RPC method. params is the raw JSON value of the
// "params" field (or nil when the request carried none). Returning an *rpcError
// forwards its code/message/data to the client; any other error is reported as
// a generic Internal error.
type Handler func(ctx context.Context, params json.RawMessage) (interface{}, error)

// Config configures a Gateway.
type Config struct {
	// Concurrency is the maximum number of method handlers that may execute
	// at the same time across all in-flight HTTP requests. Zero means no limit.
	Concurrency int
	// MaxBodyBytes caps an incoming request body. Zero means 10 MiB.
	MaxBodyBytes int64
}

// Gateway is a JSON-RPC 2.0 HTTP endpoint. It is safe for concurrent use.
type Gateway struct {
	methods map[string]Handler
	sem     chan struct{}
	maxBody int64
}

// NewGateway constructs a gateway from cfg.
func NewGateway(cfg Config) *Gateway {
	maxBody := cfg.MaxBodyBytes
	if maxBody <= 0 {
		maxBody = 10 << 20 // 10 MiB
	}
	g := &Gateway{
		methods: make(map[string]Handler),
		maxBody: maxBody,
	}
	if cfg.Concurrency > 0 {
		g.sem = make(chan struct{}, cfg.Concurrency)
	}
	return g
}

// Register binds a method name to a handler. Registering the same name twice
// replaces the previous handler.
func (g *Gateway) Register(name string, h Handler) {
	g.methods[name] = h
}

type rpcRequest struct {
	id           json.RawMessage // raw bytes to echo back; nil means notification
	notification bool
	method       string
	params       json.RawMessage // nil when the request had no params field
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	Result  interface{}     `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
	ID      json.RawMessage `json:"id"`
}

var nullID = json.RawMessage("null")

func resultResponse(id json.RawMessage, result interface{}) *rpcResponse {
	return &rpcResponse{JSONRPC: "2.0", Result: result, ID: id}
}

func errorResponse(id json.RawMessage, e *rpcError) *rpcResponse {
	return &rpcResponse{JSONRPC: "2.0", Error: e, ID: id}
}

// parseRequest validates one already syntax-checked JSON object against the
// JSON-RPC 2.0 object rules. It returns an *rpcError (always Invalid Request)
// describing the first violation, if any.
//
// IDs are kept as raw JSON bytes so string and numeric IDs are echoed back
// byte-for-byte: "1" and 1 never get conflated, and large integers are not
// rounded by float64 decoding.
func parseRequest(fields map[string]json.RawMessage) (*rpcRequest, *rpcError) {
	req := &rpcRequest{}

	rawJR, ok := fields["jsonrpc"]
	if !ok {
		return nil, errInvalidRequest("missing \"jsonrpc\" field")
	}
	var version string
	if err := decodeUseNumber(rawJR, &version); err != nil || version != "2.0" {
		return nil, errInvalidRequest("\"jsonrpc\" must be exactly \"2.0\"")
	}

	rawMethod, ok := fields["method"]
	if !ok {
		return nil, errInvalidRequest("missing \"method\" field")
	}
	// Decoding JSON null into a Go string silently yields "", so gate on the
	// token type explicitly: method must be a JSON string.
	if mb := bytes.TrimSpace(rawMethod); len(mb) == 0 || mb[0] != '"' {
		return nil, errInvalidRequest("\"method\" must be a string")
	}
	if err := decodeUseNumber(rawMethod, &req.method); err != nil {
		return nil, errInvalidRequest("\"method\" must be a string")
	}

	if rawParams, ok := fields["params"]; ok {
		trimmed := bytes.TrimSpace(rawParams)
		if len(trimmed) == 0 || (trimmed[0] != '[' && trimmed[0] != '{') {
			return nil, errInvalidRequest("\"params\" must be a JSON array or object")
		}
		req.params = rawParams
	}

	if rawID, ok := fields["id"]; ok {
		// Per spec the id, when present, must be a string, number or null.
		var id interface{}
		if err := decodeUseNumber(rawID, &id); err != nil {
			return nil, errInvalidRequest("\"id\" must be a string, number or null")
		}
		switch id.(type) {
		case string, json.Number, nil:
		default:
			return nil, errInvalidRequest("\"id\" must be a string, number or null")
		}
		req.id = bytes.TrimSpace(rawID)
	} else {
		req.notification = true
	}

	return req, nil
}

// decodeUseNumber decodes b through a decoder that keeps JSON numbers as
// json.Number instead of converting them to float64.
func decodeUseNumber(b json.RawMessage, v interface{}) error {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	if err := dec.Decode(v); err != nil {
		return err
	}
	if err := dec.Decode(&v); err != io.EOF {
		return errors.New("unexpected trailing JSON tokens")
	}
	return nil
}

// invoke runs the method handler under the global concurrency gate. Blocking
// here is what enforces the concurrency ceiling; it is interruptible by the
// request context (e.g. client disconnect).
func (g *Gateway) invoke(ctx context.Context, method string, params json.RawMessage) (interface{}, error) {
	h, ok := g.methods[method]
	if !ok {
		return nil, errMethodNotFound(method)
	}
	if g.sem != nil {
		select {
		case g.sem <- struct{}{}:
			defer func() { <-g.sem }()
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return h(ctx, params)
}

// handleSingle processes one JSON-RPC object's worth of bytes. Invalid items
// get a response with id null. Notifications return nil (no response at all).
func (g *Gateway) handleSingle(ctx context.Context, raw json.RawMessage) *rpcResponse {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return errorResponse(nullID, errInvalidRequest("request must be a JSON object"))
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(trimmed, &fields); err != nil {
		// Reached only defensively: the outer decode proved the bytes are JSON.
		return errorResponse(nullID, errInvalidRequest("request must be a JSON object"))
	}
	req, verr := parseRequest(fields)
	if verr != nil {
		// Invalid Request: the id cannot be trusted, so it is echoed as null.
		return errorResponse(nullID, verr)
	}

	result, err := g.invoke(ctx, req.method, req.params)
	if req.notification {
		// Notifications carry no id: the server must not reply, whatever the
		// handler returned (including method-not-found).
		return nil
	}
	if err != nil {
		var rpcErr *rpcError
		if errors.As(err, &rpcErr) {
			return errorResponse(req.id, rpcErr)
		}
		return errorResponse(req.id, errInternal())
	}
	return resultResponse(req.id, result)
}

// ServeHTTP implements http.Handler.
func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, "method not allowed: use POST", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(io.LimitReader(r.Body, g.maxBody+1))
	if err != nil {
		writeJSON(w, http.StatusOK, errorResponse(nullID, errParseError()))
		return
	}
	if int64(len(body)) > g.maxBody {
		http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
		return
	}

	// First pass: syntax check the whole payload exactly once. One bad token
	// anywhere means -32700 Parse error for the entire payload (single error
	// object), even if the payload was meant to be a batch.
	if !isValidJSON(body) {
		writeJSON(w, http.StatusOK, errorResponse(nullID, errParseError()))
		return
	}

	// Second pass: determine top-level shape (UseNumber keeps numeric ids exact).
	var top interface{}
	if err := decodeUseNumber(body, &top); err != nil {
		// Unreachable after isValidJSON, but keep the contract explicit.
		writeJSON(w, http.StatusOK, errorResponse(nullID, errParseError()))
		return
	}

	switch top.(type) {
	case map[string]interface{}:
		if resp := g.handleSingle(r.Context(), json.RawMessage(bytes.TrimSpace(body))); resp != nil {
			writeJSON(w, http.StatusOK, resp)
		} else {
			w.WriteHeader(http.StatusNoContent)
		}

	case []interface{}:
		var items []json.RawMessage
		// Cannot fail: syntax is valid and the top value is an array.
		_ = json.Unmarshal(body, &items)
		if len(items) == 0 {
			// An empty batch is itself an invalid Request, not an empty reply.
			writeJSON(w, http.StatusOK, errorResponse(nullID, errInvalidRequest("batch must contain at least one request")))
			return
		}

		responses := g.handleBatch(r.Context(), items)
		if len(responses) == 0 {
			// Every item was a notification: nothing to report.
			w.WriteHeader(http.StatusNoContent)
			return
		}
		writeJSON(w, http.StatusOK, responses)

	default:
		// Top-level scalar (string/number/true/false/null): valid JSON, invalid request.
		writeJSON(w, http.StatusOK, errorResponse(nullID, errInvalidRequest("request must be a JSON object or array")))
	}
}

// handleBatch executes all items concurrently and returns their responses in
// completion order, which is intentionally NOT required to match the input
// order. The JSON-RPC spec lets clients match replies by id; tests verify that
// mapping rather than array position.
func (g *Gateway) handleBatch(ctx context.Context, items []json.RawMessage) []*rpcResponse {
	responses := make([]*rpcResponse, 0, len(items))
	ch := make(chan *rpcResponse, len(items))
	var wg sync.WaitGroup
	for _, item := range items {
		wg.Add(1)
		go func(raw json.RawMessage) {
			defer wg.Done()
			ch <- g.handleSingle(ctx, raw)
		}(item)
	}
	wg.Wait()
	close(ch)
	for resp := range ch {
		if resp != nil { // nil == notification
			responses = append(responses, resp)
		}
	}
	return responses
}

// isValidJSON reports whether body is exactly one complete JSON value with no
// trailing tokens ({"a":1} extra is rejected, not silently truncated to).
func isValidJSON(body []byte) bool {
	if len(bytes.TrimSpace(body)) == 0 {
		return false
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var v interface{}
	if err := dec.Decode(&v); err != nil {
		return false
	}
	// Reject trailing tokens: a second value must not be decodable.
	if err := dec.Decode(&v); err != io.EOF {
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}
