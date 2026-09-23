// Package api 提供 REST/JSON HTTP 接口。
package api

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"revcred/internal/core"
)

// Server 是 HTTP 接口层。
type Server struct {
	svc *core.Service
}

// NewRouter 构建路由。
func NewRouter(svc *core.Service) http.Handler {
	s := &Server{svc: svc}
	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(middleware.Recoverer)

	r.Get("/healthz", s.health)

	r.Post("/v1/keys", s.createKey)
	r.Get("/v1/keys", s.listKeys)

	r.Post("/v1/credentials", s.issue)
	r.Get("/v1/credentials/{id}", s.getCredential)
	r.Post("/v1/credentials/{id}/revocations", s.revoke)
	r.Get("/v1/credentials/{id}/revocations", s.listRevocations)

	r.Post("/v1/verifications", s.verify)
	return r
}

// ---- 请求/响应类型 ----

type createKeyRequest struct {
	Issuer    string `json:"issuer"`
	ValidFrom string `json:"valid_from"` // RFC3339，可空表示现在
}

type keyResponse struct {
	Kid       string  `json:"kid"`
	Issuer    string  `json:"issuer"`
	PublicKey string  `json:"public_key"` // hex
	ValidFrom string  `json:"valid_from"`
	ValidTo   *string `json:"valid_to,omitempty"`
}

type issueRequest struct {
	Issuer    string `json:"issuer"`
	Subject   string `json:"subject"`
	Purpose   string `json:"purpose"`
	NotBefore string `json:"not_before"` // RFC3339
	NotAfter  string `json:"not_after"`  // RFC3339
	Content   string `json:"content"`
}

type credentialResponse struct {
	ID            string `json:"id"`
	Issuer        string `json:"issuer"`
	Subject       string `json:"subject"`
	Purpose       string `json:"purpose"`
	NotBefore     string `json:"not_before"`
	NotAfter      string `json:"not_after"`
	ContentDigest string `json:"content_digest"`
	Kid           string `json:"kid"`
	Signature     string `json:"signature"` // hex
	IssuedAt      string `json:"issued_at"`
}

type issueResponse struct {
	Credential credentialResponse `json:"credential"`
	Snapshot   int64              `json:"snapshot"`
}

type revokeRequest struct {
	Reason string `json:"reason"`
}

type revocationResponse struct {
	Seq          int64  `json:"seq"`
	Snapshot     int64  `json:"snapshot"` // 与 seq 相同：撤销事件即快照推进点
	CredentialID string `json:"credential_id"`
	Reason       string `json:"reason"`
	RecordedAt   string `json:"recorded_at"`
}

type verifyRequest struct {
	CredentialID string  `json:"credential_id"`
	Purpose      string  `json:"purpose"`
	At           string  `json:"at"`      // RFC3339，可空表示现在
	Content      *string `json:"content"` // 可选：提供则校验内容摘要
}

type verifyResponse struct {
	CredentialID string   `json:"credential_id"`
	Status       string   `json:"status"`
	Reasons      []string `json:"reasons"`
	Snapshot     int64    `json:"snapshot"`
	CheckedAt    string   `json:"checked_at"`
	CacheHit     bool     `json:"cache_hit"`
}

// ---- handlers ----

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	if _, err := s.svc.Snapshot(r.Context()); err != nil {
		writeError(w, http.StatusServiceUnavailable, "database unavailable")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) createKey(w http.ResponseWriter, r *http.Request) {
	var req createKeyRequest
	if !decode(w, r, &req) {
		return
	}
	if req.Issuer == "" {
		writeError(w, http.StatusBadRequest, "issuer is required")
		return
	}
	validFrom := time.Now()
	if req.ValidFrom != "" {
		t, err := time.Parse(time.RFC3339, req.ValidFrom)
		if err != nil {
			writeError(w, http.StatusBadRequest, "valid_from must be RFC3339")
			return
		}
		validFrom = t
	}
	k, err := s.svc.CreateKey(r.Context(), req.Issuer, validFrom)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, toKeyResponse(k))
}

func (s *Server) listKeys(w http.ResponseWriter, r *http.Request) {
	issuer := r.URL.Query().Get("issuer")
	if issuer == "" {
		writeError(w, http.StatusBadRequest, "issuer query parameter is required")
		return
	}
	keys, err := s.svc.ListKeys(r.Context(), issuer)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := make([]keyResponse, 0, len(keys))
	for i := range keys {
		out = append(out, toKeyResponse(&keys[i]))
	}
	writeJSON(w, http.StatusOK, map[string]any{"keys": out})
}

func (s *Server) issue(w http.ResponseWriter, r *http.Request) {
	var req issueRequest
	if !decode(w, r, &req) {
		return
	}
	nb, err := time.Parse(time.RFC3339, req.NotBefore)
	if err != nil {
		writeError(w, http.StatusBadRequest, "not_before must be RFC3339")
		return
	}
	na, err := time.Parse(time.RFC3339, req.NotAfter)
	if err != nil {
		writeError(w, http.StatusBadRequest, "not_after must be RFC3339")
		return
	}
	if req.Issuer == "" || req.Subject == "" || req.Purpose == "" {
		writeError(w, http.StatusBadRequest, "issuer, subject and purpose are required")
		return
	}
	c, snap, err := s.svc.Issue(r.Context(), core.IssueInput{
		Issuer:    req.Issuer,
		Subject:   req.Subject,
		Purpose:   req.Purpose,
		NotBefore: nb,
		NotAfter:  na,
		Content:   req.Content,
	})
	if errors.Is(err, core.ErrNoActiveKey) {
		writeError(w, http.StatusConflict, err.Error())
		return
	}
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, issueResponse{
		Credential: toCredentialResponse(c),
		Snapshot:   snap,
	})
}

func (s *Server) getCredential(w http.ResponseWriter, r *http.Request) {
	c, err := s.svc.GetCredential(r.Context(), chi.URLParam(r, "id"))
	if errors.Is(err, core.ErrNotFound) {
		writeError(w, http.StatusNotFound, "credential not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, toCredentialResponse(c))
}

func (s *Server) revoke(w http.ResponseWriter, r *http.Request) {
	var req revokeRequest
	if !decode(w, r, &req) {
		return
	}
	if req.Reason == "" {
		writeError(w, http.StatusBadRequest, "reason is required")
		return
	}
	e, err := s.svc.Revoke(r.Context(), chi.URLParam(r, "id"), req.Reason)
	if errors.Is(err, core.ErrNotFound) {
		writeError(w, http.StatusNotFound, "credential not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, toRevocationResponse(e))
}

func (s *Server) listRevocations(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	if _, err := s.svc.GetCredential(r.Context(), id); errors.Is(err, core.ErrNotFound) {
		writeError(w, http.StatusNotFound, "credential not found")
		return
	}
	events, err := s.svc.ListRevocations(r.Context(), id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := make([]revocationResponse, 0, len(events))
	for i := range events {
		out = append(out, toRevocationResponse(&events[i]))
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": out})
}

func (s *Server) verify(w http.ResponseWriter, r *http.Request) {
	var req verifyRequest
	if !decode(w, r, &req) {
		return
	}
	if req.CredentialID == "" || req.Purpose == "" {
		writeError(w, http.StatusBadRequest, "credential_id and purpose are required")
		return
	}
	var at time.Time
	if req.At != "" {
		t, err := time.Parse(time.RFC3339, req.At)
		if err != nil {
			writeError(w, http.StatusBadRequest, "at must be RFC3339")
			return
		}
		at = t
	}
	res, err := s.svc.Verify(r.Context(), core.VerifyRequest{
		CredentialID: req.CredentialID,
		Purpose:      req.Purpose,
		At:           at,
		Content:      req.Content,
	})
	if errors.Is(err, core.ErrNotFound) {
		writeError(w, http.StatusNotFound, "credential not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, verifyResponse{
		CredentialID: res.CredentialID,
		Status:       res.Status,
		Reasons:      res.Reasons,
		Snapshot:     res.Snapshot,
		CheckedAt:    res.CheckedAt.UTC().Format(time.RFC3339Nano),
		CacheHit:     res.CacheHit,
	})
}

// ---- 转换与工具 ----

func toKeyResponse(k *core.Key) keyResponse {
	resp := keyResponse{
		Kid:       k.Kid,
		Issuer:    k.Issuer,
		PublicKey: hex.EncodeToString(k.PublicKey),
		ValidFrom: k.ValidFrom.UTC().Format(time.RFC3339Nano),
	}
	if k.ValidTo != nil {
		s := k.ValidTo.UTC().Format(time.RFC3339Nano)
		resp.ValidTo = &s
	}
	return resp
}

func toCredentialResponse(c *core.Credential) credentialResponse {
	return credentialResponse{
		ID:            c.ID,
		Issuer:        c.Issuer,
		Subject:       c.Subject,
		Purpose:       c.Purpose,
		NotBefore:     c.NotBefore.UTC().Format(time.RFC3339Nano),
		NotAfter:      c.NotAfter.UTC().Format(time.RFC3339Nano),
		ContentDigest: c.ContentDigest,
		Kid:           c.Kid,
		Signature:     hex.EncodeToString(c.Signature),
		IssuedAt:      c.IssuedAt.UTC().Format(time.RFC3339Nano),
	}
}

func toRevocationResponse(e *core.RevocationEvent) revocationResponse {
	return revocationResponse{
		Seq:          e.Seq,
		Snapshot:     e.Seq,
		CredentialID: e.CredentialID,
		Reason:       e.Reason,
		RecordedAt:   e.RecordedAt.UTC().Format(time.RFC3339Nano),
	}
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	if err := json.NewDecoder(r.Body).Decode(v); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
