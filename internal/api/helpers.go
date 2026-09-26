package api

import (
	"bufio"
	"encoding/json"
	"log"
	"net"
	"net/http"
	"time"

	"idempotentsave/internal/service"
)

// faultLogger 记录故障注入导致的强制断线（默认写标准日志）。
var faultLogger = log.New(log.Writer(), "[fault] ", log.LstdFlags|log.Lmsgprefix)

func logFaultDisconnect(reason string) {
	faultLogger.Print(reason)
}

// errorBody 是统一的结构化错误响应。
type errorBody struct {
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	var b errorBody
	b.Error.Code = code
	b.Error.Message = message
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(b)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// decodeDeposit 解码存款请求；JSON 语法/类型错误作为错误返回，
// 业务语义错误（如金额为负）由 DepositRequest.Validate 处理。
func decodeDeposit(body []byte) (service.DepositRequest, error) {
	var d service.DepositRequest
	if len(body) == 0 {
		return d, errEmptyBody
	}
	if err := json.Unmarshal(body, &d); err != nil {
		return service.DepositRequest{}, err
	}
	return d, nil
}

type sentinel string

func (e sentinel) Error() string { return string(e) }

const errEmptyBody = sentinel("request body is empty")

// statusWriter 捕获状态码用于访问日志。
type statusWriter struct {
	http.ResponseWriter
	status int
	bytes  int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}

func (w *statusWriter) Write(b []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK
	}
	n, err := w.ResponseWriter.Write(b)
	w.bytes += n
	return n, err
}

// Hijack 转发底层连接劫持（故障注入"强制断线"依赖它）。
func (w *statusWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	hj, ok := w.ResponseWriter.(http.Hijacker)
	if !ok {
		return nil, nil, http.ErrNotSupported
	}
	return hj.Hijack()
}

// logRequests 是极简结构化访问日志中间件。
func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w}
		next.ServeHTTP(sw, r)
		// 被劫持的连接状态码可能为 0
		status := sw.status
		if status == 0 {
			status = 499 // client closed (nginx 风格)
		}
		log.Printf("%s %s key=%q -> %d %dB %s",
			r.Method, r.URL.Path, r.Header.Get(headerIdempotencyKey),
			status, sw.bytes, time.Since(start).Round(time.Millisecond))
	})
}
