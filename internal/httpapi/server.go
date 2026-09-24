// Package httpapi 提供纯后端 REST API（chi）。
package httpapi

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"

	"github.com/go-chi/chi/v5"

	"atomicpromo/internal/promotion"
)

type Server struct {
	svc *promotion.Service
	log *slog.Logger
}

func New(svc *promotion.Service, log *slog.Logger) *Server {
	return &Server{svc: svc, log: log}
}

func (s *Server) Router() http.Handler {
	r := chi.NewRouter()
	r.Use(requestLogger(s.log))
	r.Get("/healthz", s.health)

	r.Route("/v1", func(r chi.Router) {
		// 产物 / 证据 / 策略（不可变版本登记）
		r.Post("/artifacts", s.uploadArtifact)
		r.Post("/evidence", s.registerEvidence)
		r.Post("/policies", s.registerPolicy)
		r.Put("/tags/{name}", s.moveTag)
		r.Get("/tags/{name}", s.getTag)

		// 晋级
		r.Post("/promotions", s.promote)
		r.Post("/approvals", s.registerApproval)
		r.Post("/promotions/approve", s.completeApprovedPromotion)
		r.Post("/rollbacks", s.rollback)
		r.Post("/gc", s.collectGarbage)

		// 读模型 / 审计
		r.Get("/envs/{env}", s.getEnvironment)
		r.Get("/attempts/{id}", s.getAttempt)
		r.Get("/envs/{env}/attempts", s.listAttempts)
		r.Post("/admin/recover", s.recover) // 显式重放启动恢复（测试/运维用）
	})
	return r
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}

type errBody struct {
	Error string `json:"error"`
}

func fail(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, errBody{Error: msg})
}

// mapError 把领域错误映射成 HTTP 状态码
func mapError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, promotion.ErrBadRequest),
		errors.Is(err, promotion.ErrImmutableRef):
		fail(w, http.StatusBadRequest, err.Error())
	case errors.Is(err, promotion.ErrEvidenceReject),
		errors.Is(err, promotion.ErrPolicyReject),
		errors.Is(err, promotion.ErrApprovalReject):
		fail(w, http.StatusUnprocessableEntity, err.Error())
	case errors.Is(err, promotion.ErrRetentionReject),
		errors.Is(err, promotion.ErrRollbackTarget):
		fail(w, http.StatusConflict, err.Error())
	default:
		fail(w, http.StatusInternalServerError, err.Error())
	}
}

func decodeJSON(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	return dec.Decode(v)
}
