// Package api exposes the platform-selection service over HTTP.
package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"

	"github.example.com/ocimultipick/internal/blobstore"
	"github.example.com/ocimultipick/internal/digest"
	"github.example.com/ocimultipick/internal/oci"
	"github.example.com/ocimultipick/internal/resolver"
	"github.example.com/ocimultipick/internal/selector"
	"github.example.com/ocimultipick/internal/store"
)

// Server wires together the blob store, metadata store and resolver.
type Server struct {
	blobs *blobstore.Store
	db    *store.Store
	rsv   *resolver.Resolver
}

// NewServer constructs the API server.
func NewServer(blobs *blobstore.Store, db *store.Store) *Server {
	return &Server{blobs: blobs, db: db, rsv: resolver.New(blobs)}
}

// Router returns the fully wired chi router.
func (s *Server) Router() http.Handler {
	r := chi.NewRouter()
	r.Use(limitJSONBody)

	r.Get("/healthz", s.health)

	r.Route("/v1", func(r chi.Router) {
		r.Get("/repos", s.listRepos)
		// Repository names contain slashes (e.g. demo/app), which a single
		// chi path parameter cannot span. Mount a catch-all and split the
		// remainder into <repository>/<action> ourselves (method-aware).
		r.HandleFunc("/repos/*", s.repoDispatch)
	})

	r.NotFound(func(w http.ResponseWriter, rq *http.Request) {
		writeError(w, http.StatusNotFound, "not_found", "no such endpoint: "+rq.Method+" "+rq.URL.Path)
	})
	return r
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

const maxBodyBytes = 8 << 20 // 8 MiB

func limitJSONBody(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.Header.Get("Content-Type"), "application/json") {
			r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
		}
		next.ServeHTTP(w, r)
	})
}

// --- repositories & tags -------------------------------------------------

func (s *Server) listRepos(w http.ResponseWriter, r *http.Request) {
	repos, err := s.blobs.ListRepos()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"repositories": repos})
}

func (s *Server) listTags(w http.ResponseWriter, r *http.Request) {
	repo := chi.URLParam(r, "repo")
	if !blobstore.ValidRepo(repo) {
		writeError(w, http.StatusBadRequest, "invalid_repository", "invalid repository name")
		return
	}
	tags, err := s.db.ListTags(r.Context(), repo)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]map[string]string, 0, len(tags))
	for _, t := range tags {
		out = append(out, map[string]string{
			"tag": t.Tag, "digest": t.Digest, "mediaType": t.MediaType, "updatedAt": t.UpdatedAt,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"repository": repo, "tags": out})
}

type putTagRequest struct {
	Digest    string `json:"digest"`
	MediaType string `json:"mediaType"`
}

func (s *Server) putTag(w http.ResponseWriter, r *http.Request) {
	repo := chi.URLParam(r, "repo")
	tag := chi.URLParam(r, "tag")
	if !blobstore.ValidRepo(repo) {
		writeError(w, http.StatusBadRequest, "invalid_repository", "invalid repository name")
		return
	}
	if !blobstore.ValidTag(tag) {
		writeError(w, http.StatusBadRequest, "invalid_tag", "invalid tag name")
		return
	}
	var req putTagRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	dg, err := digest.Parse(req.Digest)
	if err != nil {
		writeError(w, http.StatusBadRequest, resolver.CodeInvalidReference, err.Error())
		return
	}
	ok, err := s.blobs.Has(repo, dg)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	if !ok {
		writeError(w, http.StatusNotFound, resolver.CodeBlobMissing, "target digest is not present locally")
		return
	}
	t := store.Tag{Repository: repo, Tag: tag, Digest: dg.String(), MediaType: req.MediaType}
	if err := s.db.UpsertTag(r.Context(), t); err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{
		"repository": repo, "tag": tag, "digest": dg.String(),
	})
}

// --- blobs ---------------------------------------------------------------

func (s *Server) getBlob(w http.ResponseWriter, r *http.Request) {
	repo := chi.URLParam(r, "repo")
	dg, err := digest.Parse(chi.URLParam(r, "digest"))
	if err != nil {
		writeError(w, http.StatusBadRequest, resolver.CodeInvalidReference, err.Error())
		return
	}
	rc, size, err := s.blobs.Open(repo, dg)
	if err != nil {
		if errors.Is(err, blobstore.ErrNotFound) {
			writeError(w, http.StatusNotFound, resolver.CodeBlobMissing, err.Error())
			return
		}
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	defer rc.Close()
	w.Header().Set("Docker-Content-Digest", dg.String())
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Length", itoa(size))
	w.WriteHeader(http.StatusOK)
	_, _ = copyBuffer(w, rc)
}

func (s *Server) putBlob(w http.ResponseWriter, r *http.Request) {
	repo := chi.URLParam(r, "repo")
	claimed, err := digest.Parse(chi.URLParam(r, "digest"))
	if err != nil {
		writeError(w, http.StatusBadRequest, resolver.CodeInvalidReference, err.Error())
		return
	}
	mediaType := r.URL.Query().Get("mediaType")
	desc, err := s.blobs.Put(repo, claimed.Algorithm(), mediaType, r.Body)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	if desc.Digest != claimed.String() {
		writeError(w, http.StatusUnprocessableEntity, resolver.CodeDigestMismatch,
			"uploaded content hashes to "+desc.Digest+", URL claims "+claimed.String())
		return
	}
	status := http.StatusCreated
	if desc.Size == 0 {
		status = http.StatusOK
	}
	writeJSON(w, status, desc)
}

// --- resolution ----------------------------------------------------------

type resolveRequest struct {
	Reference string            `json:"reference"`
	Platform  selector.Platform `json:"platform"`
}

func (s *Server) resolve(w http.ResponseWriter, r *http.Request) {
	repo := chi.URLParam(r, "repo")
	if !blobstore.ValidRepo(repo) {
		writeError(w, http.StatusBadRequest, "invalid_repository", "invalid repository name")
		return
	}
	var req resolveRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	if req.Reference == "" {
		req.Reference = "latest"
	}

	taskID := uuid.NewString()
	root, resolvedTag, tagErr := s.resolveReference(r, repo, req.Reference)

	task := &store.Task{
		ID:           taskID,
		Repository:   repo,
		Reference:    req.Reference,
		ResolvedTag:  resolvedTag,
		RootDigest:   root,
		OS:           req.Platform.OS,
		Architecture: req.Platform.Arch,
		Variant:      req.Platform.Variant,
	}

	var result *resolver.Result
	if tagErr == nil {
		res, rerr := s.rsv.Resolve(repo, mustParseDigest(root), req.Platform)
		if rerr != nil {
			recordFailure(task, rerr)
		} else {
			result = res
		}
	} else {
		recordFailure(task, tagErr)
	}

	if result != nil {
		task.Status = "succeeded"
		task.ManifestDigest = result.Manifest.Digest
		for i, c := range result.Chain {
			task.Chain = append(task.Chain, store.ChainEntry{
				Position: i, Role: c.Role, Digest: c.Digest, Size: c.Size, MediaType: c.MediaType,
			})
		}
	}

	if err := s.db.CreateTask(r.Context(), task); err != nil {
		writeError(w, http.StatusInternalServerError, "internal", "recording task: "+err.Error())
		return
	}

	resp := taskResponse(task)
	if result != nil {
		resp["resolved"] = map[string]any{
			"manifest": descriptorJSON(result.Manifest),
			"config":   descriptorJSON(result.Config),
			"layers":   descriptorsJSON(result.Layers),
			"platform": map[string]string{
				"os": result.ConfigOS, "architecture": result.ConfigArch, "variant": result.ConfigVariant,
			},
			"rootDigest": result.RootDigest.String(),
		}
		writeJSON(w, http.StatusCreated, resp)
		return
	}
	writeJSON(w, failureStatus(task.ErrorCode), resp)
}

func (s *Server) resolveReference(r *http.Request, repo, ref string) (digestStr, resolvedTag string, err error) {
	if strings.HasPrefix(ref, "sha256:") || strings.HasPrefix(ref, "sha512:") {
		dg, perr := digest.Parse(ref)
		if perr != nil {
			return "", "", resolverCodeError(resolver.CodeInvalidReference, perr.Error())
		}
		ok, herr := s.blobs.Has(repo, dg)
		if herr != nil {
			return "", "", resolverCodeError("internal", herr.Error())
		}
		if !ok {
			return "", "", resolverCodeError(resolver.CodeBlobMissing, "digest "+ref+" is not present locally")
		}
		return dg.String(), "", nil
	}
	t, gerr := s.db.GetTag(r.Context(), repo, ref)
	if gerr != nil {
		if errors.Is(gerr, store.ErrNotFound) {
			return "", "", resolverCodeError("tag_not_found", gerr.Error())
		}
		return "", "", resolverCodeError("internal", gerr.Error())
	}
	return t.Digest, t.Digest, nil
}

// --- tasks ---------------------------------------------------------------

func (s *Server) getTask(w http.ResponseWriter, r *http.Request) {
	repo := chi.URLParam(r, "repo")
	task, err := s.db.GetTask(r.Context(), chi.URLParam(r, "id"))
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, "task_not_found", err.Error())
			return
		}
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	if task.Repository != repo {
		writeError(w, http.StatusNotFound, "task_not_found", "task does not belong to repository "+repo)
		return
	}
	writeJSON(w, http.StatusOK, taskResponse(task))
}

func (s *Server) listTasks(w http.ResponseWriter, r *http.Request) {
	repo := chi.URLParam(r, "repo")
	limit := 50
	if l := r.URL.Query().Get("limit"); l != "" {
		if n := atoi(l); n > 0 && n <= 500 {
			limit = n
		}
	}
	tasks, err := s.db.ListTasks(r.Context(), repo, limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]map[string]any, 0, len(tasks))
	for _, t := range tasks {
		out = append(out, taskResponse(t))
	}
	writeJSON(w, http.StatusOK, map[string]any{"repository": repo, "tasks": out})
}

// --- helpers -------------------------------------------------------------

func recordFailure(task *store.Task, err error) {
	task.Status = "failed"
	var re *resolver.Error
	if errors.As(err, &re) {
		task.ErrorCode = re.Code
		task.ErrorDetail = re.Detail
	} else {
		task.ErrorCode = "internal"
		task.ErrorDetail = err.Error()
	}
}

func failureStatus(code string) int {
	switch code {
	case resolver.CodeBlobMissing, resolver.CodeNoMatch, "tag_not_found":
		return http.StatusNotFound
	case resolver.CodeAmbiguous:
		return http.StatusConflict
	case resolver.CodeInvalidReference, "invalid_json", "invalid_repository":
		return http.StatusBadRequest
	case "internal":
		return http.StatusInternalServerError
	default:
		// Verification failures, cycles, config mismatches etc: the content is
		// present but structurally invalid.
		return http.StatusUnprocessableEntity
	}
}

func taskResponse(t *store.Task) map[string]any {
	chain := make([]map[string]any, 0, len(t.Chain))
	for _, c := range t.Chain {
		chain = append(chain, map[string]any{
			"position": c.Position, "role": c.Role, "digest": c.Digest,
			"size": c.Size, "mediaType": c.MediaType,
		})
	}
	resp := map[string]any{
		"id":         t.ID,
		"repository": t.Repository,
		"reference":  map[string]string{"requested": t.Reference, "resolvedDigest": t.RootDigest},
		"platform": map[string]string{
			"os": t.OS, "architecture": t.Architecture, "variant": t.Variant,
		},
		"status":          t.Status,
		"createdAt":       t.CreatedAt,
		"dependencyChain": chain,
	}
	if t.ResolvedTag != "" {
		resp["reference"].(map[string]string)["boundFromTag"] = t.ResolvedTag
	}
	if t.Status != "succeeded" {
		resp["error"] = map[string]string{"code": t.ErrorCode, "detail": t.ErrorDetail}
	}
	return resp
}

func descriptorJSON(d oci.Descriptor) map[string]any {
	return map[string]any{
		"mediaType": d.MediaType, "digest": d.Digest, "size": d.Size,
	}
}

func descriptorsJSON(ds []oci.Descriptor) []map[string]any {
	out := make([]map[string]any, 0, len(ds))
	for _, d := range ds {
		out = append(out, descriptorJSON(d))
	}
	return out
}

func resolverCodeError(code, detail string) *resolver.Error {
	return &resolver.Error{Code: code, Detail: detail}
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func writeError(w http.ResponseWriter, status int, code, detail string) {
	writeJSON(w, status, map[string]any{"error": map[string]string{"code": code, "detail": detail}})
}
