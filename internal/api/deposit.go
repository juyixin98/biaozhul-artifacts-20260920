package api

import (
	"errors"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"idempotentsave/internal/store"
)

const maxBodyBytes = 1 << 20 // 1 MiB

// 故障注入请求头（仅用于本项目的本地验收，任何部署都不应信任它们，
// 但因为整个系统不接生产，这里直接在协议中提供）。
const (
	headerBeforeCommitDisconnect = "X-Fault-Before-Commit-Disconnect"
	headerAfterCommitDisconnect  = "X-Fault-After-Commit-Disconnect"
	headerDelayBeforeCommitMS    = "X-Fault-Delay-Before-Commit-Ms"
	headerCrashBeforeCommit      = "X-Fault-Crash-Before-Commit"
	headerIdempotencyKey         = "Idempotency-Key"
)

func (s *Server) handleDeposit(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use POST")
		return
	}
	key := strings.TrimSpace(r.Header.Get(headerIdempotencyKey))
	if key == "" {
		writeError(w, http.StatusBadRequest, "missing_idempotency_key",
			"send an Idempotency-Key header; the key is bound to the request digest")
		return
	}

	body, err := io.ReadAll(io.LimitReader(r.Body, maxBodyBytes+1))
	if err != nil {
		writeError(w, http.StatusBadRequest, "unreadable_body", err.Error())
		return
	}
	if len(body) > maxBodyBytes {
		writeError(w, http.StatusRequestEntityTooLarge, "body_too_large", "request body exceeds 1 MiB")
		return
	}

	dep, decodeErr := decodeDeposit(body)

	lease, existing, err := s.cfg.Service.Begin(key, dep, body)
	if err != nil {
		s.handleBeginError(w, r, key, err)
		return
	}
	if existing != nil {
		// 键已有完成记录：原样重放保存的状态码与响应体，不执行任何副作用。
		writeReplay(w, *existing)
		return
	}

	// 本请求获得执行权。
	if decodeErr != nil {
		// 入站错误同样进入"结果存储"：同键重试得到一致的 422，而不是时好时坏。
		rec, err := s.cfg.Service.CommitValidationFailure(lease, decodeErr.Error())
		if err != nil {
			writeError(w, http.StatusInternalServerError, "commit_failed", err.Error())
			return
		}
		writeReplay(w, rec)
		return
	}
	if verr := dep.Validate(); verr != nil {
		rec, err := s.cfg.Service.CommitValidationFailure(lease, verr.Error())
		if err != nil {
			writeError(w, http.StatusInternalServerError, "commit_failed", err.Error())
			return
		}
		writeReplay(w, rec)
		return
	}

	calls, exErr := s.cfg.Service.RunExternal(r.Context(), lease, dep)
	if exErr != nil {
		// 外部步骤失败：副作用尚未提交，释放占位并明确告知调用方可安全重试。
		if relErr := s.cfg.Service.FailAndRelease(lease); relErr != nil {
			writeError(w, http.StatusInternalServerError, "release_failed", relErr.Error())
			return
		}
		writeError(w, http.StatusBadGateway, "external_step_failed_retryable",
			"external audit call failed before the local transaction: "+exErr.Error()+
				"; the idempotency slot was released, retry with the same key")
		return
	}
	_ = calls

	// —— 故障点：提交前延迟（配合并发重复请求，观察"处理中"响应）——
	if ms := faultDelayMS(r); ms > 0 {
		logFaultDisconnect("fault: sleep " + strconv.Itoa(ms) + "ms before commit")
		time.Sleep(time.Duration(ms) * time.Millisecond)
	}

	// —— 故障点：提交前崩溃（进程立即退出；pending 已 fsync，重启后仍在）——
	if isFaultSet(r, headerCrashBeforeCommit) {
		logFaultDisconnect("fault: crash before commit (os.Exit)")
		go os.Exit(2) // 放到 goroutine 里让当前请求先停止推进
		select {}     // 永久阻塞本请求，直到进程退出
	}

	// —— 故障点：提交前断线（模拟执行到此处连接死亡，占位遗留至 TTL）——
	if isFaultSet(r, headerBeforeCommitDisconnect) {
		s.abandonConnection(w, "fault: disconnect before commit")
		return
	}

	rec, resp, err := s.cfg.Service.Commit(lease, dep, calls)
	if err != nil {
		if errors.Is(err, store.ErrLeaseLost) {
			// 占位已被 TTL 回收并由更新的尝试接管：本次结果必须丢弃。
			writeError(w, http.StatusConflict, "lease_lost",
				"a newer attempt took over this idempotency key; retry to obtain its result")
			return
		}
		writeError(w, http.StatusInternalServerError, "commit_failed", err.Error())
		return
	}

	// —— 故障点：提交后断线（事务已落盘，但响应在回程丢失）——
	if isFaultSet(r, headerAfterCommitDisconnect) {
		s.abandonConnection(w, "fault: disconnect after commit (tx "+resp.TxID+" already durable)")
		return
	}

	w.Header().Set("Idempotency-Status", "created")
	w.Header().Set("Idempotency-Generation", itoa(rec.Gen))
	writeJSON(w, http.StatusOK, resp)
}

// handleBeginError 处理占位阶段的三种非成功结局。
func (s *Server) handleBeginError(w http.ResponseWriter, r *http.Request, key string, err error) {
	switch {
	case errors.Is(err, store.ErrConflict):
		writeError(w, http.StatusConflict, "idempotency_conflict",
			"this Idempotency-Key is already bound to a different request digest; use a unique key per logical request")
		return
	case errors.Is(err, store.ErrInProgress):
		s.awaitInProgress(w, r, key)
		return
	default:
		writeError(w, http.StatusInternalServerError, "begin_failed", err.Error())
	}
}

// awaitInProgress 对"处理中重复请求"给出明确响应：
// 在有限时间内长轮询首个尝试的结局——期间提交则直接重放结果；
// 超时仍未完成则返回 202 + Retry-After，告知稍后用同键查询/重试。
func (s *Server) awaitInProgress(w http.ResponseWriter, r *http.Request, key string) {
	committed := s.cfg.Service.WaitForCompletion(r.Context(), key, s.cfg.WaitMax)
	if committed {
		if rec, ok := s.cfg.Service.Lookup(key); ok && rec.Status == store.Completed {
			writeReplay(w, rec)
			return
		}
	}
	// 仍在处理（或首个尝试已释放）：明确响应，不制造模糊的 5xx。
	w.Header().Set("Retry-After", "1")
	writeError(w, http.StatusAccepted, "in_progress",
		"the first request with this key is still processing; retry with the SAME key and body, "+
			"or GET /v1/idempotency/<key> to inspect")
}

// writeReplay 以原始状态码重放已保存的结果，并显式标注这是重放。
func writeReplay(w http.ResponseWriter, rec store.Record) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Idempotency-Status", "replayed")
	w.Header().Set("Idempotency-Generation", itoa(rec.Gen))
	if rec.StatusCode == 0 {
		rec.StatusCode = http.StatusOK
	}
	w.WriteHeader(rec.StatusCode)
	if len(rec.Response) > 0 {
		_, _ = w.Write(rec.Response)
	}
}

func isFaultSet(r *http.Request, header string) bool {
	v := strings.TrimSpace(strings.ToLower(r.Header.Get(header)))
	return v == "1" || v == "true" || v == "yes"
}

// faultDelayMS 解析提交前延迟故障头，非法值视为未设置。
func faultDelayMS(r *http.Request) int {
	v := strings.TrimSpace(r.Header.Get(headerDelayBeforeCommitMS))
	if v == "" {
		return 0
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < 0 {
		return 0
	}
	if n > 60000 {
		n = 60000
	}
	return n
}

// abandonConnection 直接劫持并关闭底层 TCP 连接，不写任何 HTTP 响应，
// 模拟"客户端在拿到响应前断线/对端进程消失"。
func (s *Server) abandonConnection(w http.ResponseWriter, reason string) {
	hj, ok := w.(http.Hijacker)
	if !ok {
		// 不支持劫持时退化为服务端静默断开（flush 空响应后关闭）。
		w.Header().Set("Connection", "close")
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	conn, brw, err := hj.Hijack()
	if err != nil {
		w.Header().Set("Connection", "close")
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	_ = brw.Writer.Flush()
	_ = conn.Close()
	logFaultDisconnect(reason)
}

func itoa(u uint64) string {
	return strconv.FormatUint(u, 10)
}
