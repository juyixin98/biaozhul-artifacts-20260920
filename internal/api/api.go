// Package api 是渐进发布判定器的 HTTP 接口（Chi 路由）。
package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

	"rollout/internal/engine"
	"rollout/internal/store"
)

// Fetcher 从指标服务拉取观察区间。生产用 metrics.Client，测试可注入桩。
type Fetcher interface {
	Window(ctx context.Context, service string, from, to time.Time) (*engine.MetricsWindow, error)
}

type Server struct {
	Store   *store.Store
	Metrics Fetcher
	Now     func() time.Time // 可注入时钟
}

func NewRouter(s *Server) http.Handler {
	r := chi.NewRouter()
	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	r.Post("/rollouts", s.createRollout)
	r.Get("/rollouts", s.listRollouts)
	r.Get("/rollouts/{id}", s.getRollout)
	r.Post("/rollouts/{id}/evaluate", s.evaluate)
	r.Post("/rollouts/{id}/commands", s.command)
	r.Get("/rollouts/{id}/decisions", s.listDecisions)
	return r
}

// ---------- 请求/响应 ----------

type createRolloutReq struct {
	Name                     string  `json:"name"`
	Service                  string  `json:"service"`
	MinSamples               int64   `json:"min_samples"`
	ObservationWindowSeconds int64   `json:"observation_window_seconds"`
	MaxErrorRate             float64 `json:"max_error_rate"`
	MaxP95LatencyMs          float64 `json:"max_p95_latency_ms"`
	Note                     string  `json:"note"`
}

type commandReq struct {
	IdempotencyKey     string `json:"idempotency_key"`
	Type               string `json:"type"` // promote | pause | rollback
	ExpectedGeneration *int64 `json:"expected_generation"`
}

type commandResponse struct {
	Rollout          *store.Rollout  `json:"rollout"`
	Result           string          `json:"result"` // applied | rejected_conflict | rejected_state
	Detail           string          `json:"detail,omitempty"`
	Decision         *store.Decision `json:"decision,omitempty"`
	IdempotentReplay bool            `json:"idempotent_replay"`
}

// ---------- handlers ----------

func (s *Server) createRollout(w http.ResponseWriter, r *http.Request) {
	var req createRolloutReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if req.Name == "" || req.Service == "" {
		writeErr(w, http.StatusBadRequest, "name and service are required")
		return
	}
	if req.MinSamples <= 0 || req.ObservationWindowSeconds <= 0 {
		writeErr(w, http.StatusBadRequest, "min_samples and observation_window_seconds must be > 0")
		return
	}
	if req.MaxErrorRate <= 0 || req.MaxErrorRate > 1 || req.MaxP95LatencyMs <= 0 {
		writeErr(w, http.StatusBadRequest, "max_error_rate must be in (0,1], max_p95_latency_ms > 0")
		return
	}
	rl, err := s.Store.CreateRollout(r.Context(), store.CreateRolloutParams{
		Name:                     req.Name,
		Service:                  req.Service,
		MinSamples:               req.MinSamples,
		ObservationWindowSeconds: req.ObservationWindowSeconds,
		MaxErrorRate:             req.MaxErrorRate,
		MaxP95LatencyMs:          req.MaxP95LatencyMs,
		Note:                     req.Note,
	})
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, rl)
}

func (s *Server) listRollouts(w http.ResponseWriter, r *http.Request) {
	rls, err := s.Store.ListRollouts(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, rls)
}

func (s *Server) getRollout(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(w, r)
	if !ok {
		return
	}
	rl, err := s.Store.GetRollout(r.Context(), id)
	if err != nil {
		writeRolloutError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rl)
}

func (s *Server) listDecisions(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(w, r)
	if !ok {
		return
	}
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	if _, err := s.Store.GetRollout(r.Context(), id); err != nil {
		writeRolloutError(w, err)
		return
	}
	ds, err := s.Store.ListDecisions(r.Context(), id, limit)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, ds)
}

// runEvaluation 拉取当前阶段观察区间指标并执行纯判定（不落库）。
func (s *Server) runEvaluation(ctx context.Context, rl *store.Rollout) (engine.Decision, time.Time, time.Time, bool, error) {
	now := s.Now()
	windowEnd := rl.StageStartedAt.Add(time.Duration(rl.ObservationWindowSeconds) * time.Second)
	complete := !now.Before(windowEnd)
	to := now
	if windowEnd.Before(to) {
		to = windowEnd // 只取本阶段区间，不看之后
	}
	w, err := s.Metrics.Window(ctx, rl.Service, rl.StageStartedAt, to)
	if err != nil {
		// 拉取失败按指标缺失处理（未知），并记录错误，不当作健康
		w = nil
	}
	d := engine.Evaluate(engine.Input{
		StageIdx:       rl.StageIdx,
		MinSamples:     rl.MinSamples,
		WindowComplete: complete,
		Window:         w,
		Threshold:      rl.Threshold,
	})
	return d, rl.StageStartedAt, windowEnd, complete, err
}

func (s *Server) evaluate(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(w, r)
	if !ok {
		return
	}
	rl, err := s.Store.GetRollout(r.Context(), id)
	if err != nil {
		writeRolloutError(w, err)
		return
	}
	// 任意状态都可做只读判定取证（例如回退后观察迟到成功数据）
	d, ws, we, complete, fetchErr := s.runEvaluation(r.Context(), rl)
	rec, err := s.Store.InsertDecision(r.Context(), s.Store.Pool, rl, &d, ws, we, complete)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	resp := map[string]any{"rollout": rl, "decision": rec}
	if fetchErr != nil {
		resp["metrics_fetch_error"] = fetchErr.Error()
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) command(w http.ResponseWriter, r *http.Request) {
	id, ok := idParam(w, r)
	if !ok {
		return
	}
	var req commandReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if req.IdempotencyKey == "" {
		writeErr(w, http.StatusBadRequest, "idempotency_key is required")
		return
	}
	switch req.Type {
	case "promote", "pause", "rollback":
	default:
		writeErr(w, http.StatusBadRequest, "type must be promote, pause or rollback")
		return
	}
	if req.ExpectedGeneration == nil {
		writeErr(w, http.StatusBadRequest, "expected_generation is required")
		return
	}

	reqHash := requestHash(req)

	// 幂等重放：先查已存命令，存在则原样返回，绝不重复执行
	if existing, err := s.Store.FindCommand(r.Context(), s.Store.Pool, id, req.IdempotencyKey); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	} else if existing != nil {
		if existing.RequestHash != reqHash {
			writeErr(w, http.StatusConflict,
				"idempotency_key reused with a different request body (SHA-256 request hash mismatch)")
			return
		}
		w.Header().Set("Idempotent-Replay", "true")
		writeJSON(w, existing.HTTPStatus, json.RawMessage(existing.Response))
		return
	}

	ctx := r.Context()
	tx, err := s.Store.Pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer tx.Rollback(ctx)

	// 行锁串行化所有命令
	rl, err := s.Store.GetRolloutForUpdate(ctx, tx, id)
	if err != nil {
		writeRolloutError(w, err)
		return
	}

	// 并发竞争：拿锁后再查一次幂等键
	if existing, err := s.Store.FindCommand(ctx, tx, id, req.IdempotencyKey); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	} else if existing != nil {
		if existing.RequestHash != reqHash {
			writeErr(w, http.StatusConflict,
				"idempotency_key reused with a different request body (SHA-256 request hash mismatch)")
			return
		}
		w.Header().Set("Idempotent-Replay", "true")
		writeJSON(w, existing.HTTPStatus, json.RawMessage(existing.Response))
		return
	}

	// 乐观并发：代次不匹配拒绝，不执行
	if rl.Generation != *req.ExpectedGeneration {
		resp := map[string]any{
			"result":              "rejected_conflict",
			"detail":              "expected generation does not match current generation",
			"rollout":             rl,
			"expected_generation": *req.ExpectedGeneration,
			"current_generation":  rl.Generation,
		}
		_ = s.Store.InsertCommand(ctx, tx, id, req.Type, *req.ExpectedGeneration,
			req.IdempotencyKey, reqHash, "rejected_conflict", http.StatusConflict, resp)
		if err := tx.Commit(ctx); err != nil {
			writeErr(w, http.StatusInternalServerError, err.Error())
			return
		}
		writeJSON(w, http.StatusConflict, resp)
		return
	}

	var (
		result     string
		httpStatus = http.StatusOK
		detail     string
		decRec     *store.Decision
	)
	switch req.Type {
	case "pause":
		if rl.Status != "active" {
			result, httpStatus, detail = "rejected_state", http.StatusConflict,
				"can only pause an active rollout (current: "+rl.Status+")"
		} else {
			if err := s.Store.SetStatus(ctx, tx, id, "paused"); err != nil {
				writeErr(w, http.StatusInternalServerError, err.Error())
				return
			}
			result = "applied"
		}
	case "rollback":
		if rl.Status == "rolled_back" || rl.Status == "completed" {
			result, httpStatus, detail = "rejected_state", http.StatusConflict,
				"cannot rollback a "+rl.Status+" rollout"
		} else {
			if err := s.Store.SetStatus(ctx, tx, id, "rolled_back"); err != nil {
				writeErr(w, http.StatusInternalServerError, err.Error())
				return
			}
			result = "applied"
		}
	case "promote":
		if rl.Status != "active" {
			result, httpStatus, detail = "rejected_state", http.StatusConflict,
				"can only promote an active rollout (current: "+rl.Status+")"
		} else {
			// 推进前即时重新判定：完整观察窗口 + 最小样本 + 指标达标
			d, ws, we, complete, fetchErr := s.runEvaluation(ctx, rl)
			rec, derr := s.Store.InsertDecision(ctx, tx, rl, &d, ws, we, complete)
			if derr != nil {
				writeErr(w, http.StatusInternalServerError, derr.Error())
				return
			}
			decRec = rec
			if fetchErr != nil {
				detail = "metrics fetch failed: " + fetchErr.Error()
			}
			if d.CanPromote {
				next, done, _ := engine.NextStage(rl.StageIdx)
				if err := s.Store.AdvanceStage(ctx, tx, id, next, done); err != nil {
					writeErr(w, http.StatusInternalServerError, err.Error())
					return
				}
				result = "applied"
			} else {
				result = "rejected_state"
				httpStatus = http.StatusConflict
				if detail == "" {
					detail = "promotion blocked: " + d.Verdict + " (" + d.Reason + ")"
				}
			}
		}
	}

	updated, err := s.Store.GetRolloutForUpdate(ctx, tx, id)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	resp := commandResponse{
		Rollout: updated, Result: result, Detail: detail,
		Decision: decRec, IdempotentReplay: false,
	}
	cmdStatus := "applied"
	if result != "applied" {
		cmdStatus = result // rejected_conflict | rejected_state
	}
	if err := s.Store.InsertCommand(ctx, tx, id, req.Type, *req.ExpectedGeneration,
		req.IdempotencyKey, reqHash, cmdStatus, httpStatus, resp); err != nil {
		if store.IsUniqueViolation(err) {
			// 并发幂等竞争：读取对方已存结果重放
			existing, ferr := s.Store.FindCommand(ctx, tx, id, req.IdempotencyKey)
			if ferr != nil || existing == nil {
				writeErr(w, http.StatusInternalServerError, "idempotency race")
				return
			}
			_ = tx.Rollback(ctx)
			w.Header().Set("Idempotent-Replay", "true")
			writeJSON(w, existing.HTTPStatus, json.RawMessage(existing.Response))
			return
		}
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	if err := tx.Commit(ctx); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, httpStatus, resp)
}

// ---------- helpers ----------

func idParam(w http.ResponseWriter, r *http.Request) (int64, bool) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "invalid rollout id")
		return 0, false
	}
	return id, true
}

// requestHash 对命令语义体计算 SHA-256 指纹（真实密码学哈希），
// 用于发现“同一幂等键、不同请求体”的异常用法。
func requestHash(req commandReq) string {
	canonical := struct {
		Type               string `json:"type"`
		ExpectedGeneration *int64 `json:"expected_generation"`
	}{Type: req.Type, ExpectedGeneration: req.ExpectedGeneration}
	b, _ := json.Marshal(canonical)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func writeRolloutError(w http.ResponseWriter, err error) {
	if errors.Is(err, pgx.ErrNoRows) {
		writeErr(w, http.StatusNotFound, "rollout not found")
		return
	}
	writeErr(w, http.StatusInternalServerError, err.Error())
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}
