package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5"

	"atomicpromo/internal/approval"
	"atomicpromo/internal/blob"
	"atomicpromo/internal/evidence"
	"atomicpromo/internal/policy"
	"atomicpromo/internal/promotion"
	"atomicpromo/internal/store"
)

// uploadArtifact: multipart/form-data, 字段 content=<文件>，可选 digest=sha256:...
func (s *Server) uploadArtifact(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseMultipartForm(64 << 20); err != nil {
		fail(w, http.StatusBadRequest, "expected multipart form with 'content' file: "+err.Error())
		return
	}
	f, _, err := r.FormFile("content")
	if err != nil {
		fail(w, http.StatusBadRequest, "missing form file 'content'")
		return
	}
	defer f.Close()
	declared := r.FormValue("digest")
	ctx := withFault(r.Context(), r)
	digest, size, err := s.svc.UploadArtifact(ctx, f, declared)
	if err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"digest": digest, "size_bytes": size,
		"note": "content-addressed: digest was recomputed over the actual bytes",
	})
}

type registerEvidenceReq struct {
	evidence.Envelope
}

func (s *Server) registerEvidence(w http.ResponseWriter, r *http.Request) {
	var req evidence.Envelope
	if err := decodeJSON(r, &req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	if err := s.svc.RegisterEvidence(r.Context(), req); err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"evidence_id": req.EvidenceID, "version": req.Version,
		"artifact_digest": req.ArtifactDigest, "immutable": true,
	})
}

func (s *Server) registerPolicy(w http.ResponseWriter, r *http.Request) {
	var req policy.SignedBody
	if err := decodeJSON(r, &req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	hash, err := s.svc.RegisterPolicy(r.Context(), req)
	if err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"policy_id": req.Body.PolicyID, "version": req.Body.Version,
		"body_sha256": hash, "immutable": true,
	})
}

type moveTagReq struct {
	Digest string `json:"digest"`
}

func (s *Server) moveTag(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "name")
	var req moveTagReq
	if err := decodeJSON(r, &req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	if err := s.svc.MoveTag(r.Context(), name, req.Digest); err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"name": name, "digest": req.Digest,
		"warning": "tags float; the promotion API rejects tag references"})
}

func (s *Server) getTag(w http.ResponseWriter, r *http.Request) {
	t, err := s.svc.GetTag(r.Context(), chi.URLParam(r, "name"))
	if err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *Server) promote(w http.ResponseWriter, r *http.Request) {
	var req promotion.PromoteRequest
	if err := decodeJSON(r, &req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	out, err := s.svc.Promote(withFault(r.Context(), r), req)
	if err != nil {
		mapError(w, err)
		return
	}
	code := http.StatusOK
	if out.Status == "committed" {
		code = http.StatusCreated
	}
	writeJSON(w, code, out)
}

func (s *Server) registerApproval(w http.ResponseWriter, r *http.Request) {
	var d approval.Decision
	if err := decodeJSON(r, &d); err != nil {
		fail(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	if err := s.svc.RegisterApproval(r.Context(), d); err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"approval_id": d.ApprovalID, "status": "valid",
	})
}

type completeReq struct {
	ApprovalID     string  `json:"approval_id"`
	IdempotencyKey *string `json:"idempotency_key,omitempty"`
}

func (s *Server) completeApprovedPromotion(w http.ResponseWriter, r *http.Request) {
	var req completeReq
	if err := decodeJSON(r, &req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	if req.ApprovalID == "" {
		fail(w, http.StatusBadRequest, "approval_id required")
		return
	}
	idem := ""
	if req.IdempotencyKey != nil {
		idem = *req.IdempotencyKey
	}
	out, err := s.svc.CompleteApprovedPromotion(withFault(r.Context(), r), req.ApprovalID, idem)
	if err != nil {
		mapError(w, err)
		return
	}
	code := http.StatusOK
	if out.Status == "committed" {
		code = http.StatusCreated
	}
	writeJSON(w, code, out)
}

func (s *Server) rollback(w http.ResponseWriter, r *http.Request) {
	var req promotion.RollbackRequest
	if err := decodeJSON(r, &req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	out, err := s.svc.Rollback(withFault(r.Context(), r), req)
	if err != nil {
		mapError(w, err)
		return
	}
	code := http.StatusOK
	if out.Status == "committed" {
		code = http.StatusCreated
	}
	writeJSON(w, code, out)
}

type gcReq struct {
	Env           string `json:"env"`
	PolicyID      string `json:"policy_id"`
	PolicyVersion int64  `json:"policy_version"`
}

func (s *Server) collectGarbage(w http.ResponseWriter, r *http.Request) {
	var req gcReq
	if err := decodeJSON(r, &req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	res, err := s.svc.CollectGarbage(r.Context(), req.Env, req.PolicyID, req.PolicyVersion)
	if err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) getEnvironment(w http.ResponseWriter, r *http.Request) {
	v, err := s.svc.GetEnvironment(r.Context(), chi.URLParam(r, "env"))
	if err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) getAttempt(w http.ResponseWriter, r *http.Request) {
	a, err := s.svc.GetAttempt(r.Context(), chi.URLParam(r, "id"))
	if err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, attemptView(a))
}

func (s *Server) listAttempts(w http.ResponseWriter, r *http.Request) {
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	list, err := s.svc.ListAttempts(r.Context(), chi.URLParam(r, "env"), limit)
	if err != nil {
		mapError(w, err)
		return
	}
	views := make([]map[string]any, 0, len(list))
	for i := range list {
		views = append(views, attemptView(&list[i]))
	}
	writeJSON(w, http.StatusOK, map[string]any{"attempts": views})
}

// attemptView 把行结构转成 API JSON，并把存为文本的 receipt_json 展开成对象
func attemptView(a *store.AttemptRow) map[string]any {
	b, err := json.Marshal(a)
	if err != nil {
		return map[string]any{"error": err.Error()}
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		return map[string]any{"error": err.Error()}
	}
	if raw, ok := m["receipt_json"].(string); ok && raw != "" {
		var rcpt any
		if json.Unmarshal([]byte(raw), &rcpt) == nil {
			m["receipt"] = rcpt
		}
	}
	delete(m, "receipt_json")
	return m
}

func (s *Server) recover(w http.ResponseWriter, r *http.Request) {
	rep, err := s.svc.RecoverOnStartup(r.Context())
	if err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

// ---- 故障注入（仅当 ALLOW_FAULT_INJECTION=1 时生效，供验收测试使用） ----

func withFault(ctx context.Context, r *http.Request) context.Context {
	if r == nil || faultInjectionAllowed == "" {
		return ctx
	}
	mode := r.Header.Get("X-Test-Fault")
	if mode == "" {
		return ctx
	}
	f := &blob.Fault{}
	switch mode {
	case "corrupt-copy":
		f.CorruptBytes = 4 // 在复制流第 4 字节处翻转
	case "crash-before-commit":
		f.CrashBeforePointerCommit = true
		f.OnCrash = crashProcess
	default:
		return ctx
	}
	return blob.WithFault(ctx, f)
}

var faultInjectionAllowed = getenvDefault("ALLOW_FAULT_INJECTION", "")

var errFaultDisabled = errors.New("fault injection disabled")
