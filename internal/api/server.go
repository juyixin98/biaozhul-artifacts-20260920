// Package api exposes the multi-arch selection service over HTTP.
package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"ociarch/internal/oci"
	"ociarch/internal/store"
)

type Server struct {
	st *store.Store
}

func NewRouter(st *store.Store) http.Handler {
	s := &Server{st: st}
	r := chi.NewRouter()
	r.Use(middleware.Logger)
	r.Use(middleware.Recoverer)
	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	r.Route("/api/v1", func(r chi.Router) {
		r.Post("/import", s.handleImport)
		r.Post("/resolve", s.handleResolve)
		r.Get("/resolutions", s.handleListResolutions)
		r.Get("/resolutions/{id}", s.handleGetResolution)
		r.Get("/tags", s.handleListTags)
		// Tag names contain slashes ("demo/app:latest"), so the history
		// route uses a wildcard and strips the "/history" suffix.
		r.Get("/tags/*", s.handleTagHistory)
	})
	return r
}

type apiError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code string, err error) {
	writeJSON(w, status, map[string]any{"error": apiError{Code: code, Message: err.Error()}})
}

// ---- import ----

type importRequest struct {
	Path string `json:"path"` // local OCI layout directory (index.json + blobs/)
	Tag  string `json:"tag"`  // e.g. "demo/app:latest"; movable pointer
}

func (s *Server) handleImport(w http.ResponseWriter, r *http.Request) {
	var req importRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "bad_json", err)
		return
	}
	if req.Path == "" || req.Tag == "" {
		writeErr(w, http.StatusBadRequest, "bad_request",
			fmt.Errorf("both 'path' and 'tag' are required"))
		return
	}
	if strings.Contains(req.Path, "..") {
		writeErr(w, http.StatusBadRequest, "bad_request",
			fmt.Errorf("path must not contain '..'"))
		return
	}
	g, err := oci.ImportLayout(req.Path)
	if err != nil {
		// Verification failures (digest/size mismatch, wrong config
		// platform, missing layer, circular reference) land here.
		writeErr(w, http.StatusUnprocessableEntity, "verification_failed", err)
		return
	}
	if err := s.st.SaveGraph(g, req.Tag); err != nil {
		writeErr(w, http.StatusInternalServerError, "store_failed", err)
		return
	}
	manifests := 0
	for _, e := range g.Edges {
		if e.Kind == oci.EdgeManifest {
			manifests++
		}
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"tag":         req.Tag,
		"root_digest": g.Root,
		"nodes":       len(g.Nodes),
		"manifests":   manifests,
	})
}

// ---- resolve ----

type resolveRequest struct {
	Ref      string `json:"ref"` // tag ("repo:tag") or digest ("sha256:...")
	Platform struct {
		OS           string `json:"os"`
		Architecture string `json:"architecture"`
		Variant      string `json:"variant"`
	} `json:"platform"`
}

func (s *Server) handleResolve(w http.ResponseWriter, r *http.Request) {
	var req resolveRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "bad_json", err)
		return
	}
	if req.Ref == "" || req.Platform.OS == "" || req.Platform.Architecture == "" {
		writeErr(w, http.StatusBadRequest, "bad_request",
			fmt.Errorf("'ref', 'platform.os' and 'platform.architecture' are required"))
		return
	}

	// A tag is a movable pointer: bind it to its current digest NOW. The
	// resolution task records that digest, so a later tag move never
	// changes what this task resolved. Same tag != same artifact.
	var rootDigest string
	if strings.HasPrefix(req.Ref, "sha256:") {
		ok, err := s.st.HasNode(req.Ref)
		if err != nil {
			writeErr(w, http.StatusInternalServerError, "store_failed", err)
			return
		}
		if !ok {
			writeErr(w, http.StatusNotFound, "unknown_digest",
				fmt.Errorf("digest %s was never imported", req.Ref))
			return
		}
		rootDigest = req.Ref
	} else {
		d, err := s.st.TagDigest(req.Ref)
		if err != nil {
			writeErr(w, http.StatusNotFound, "unknown_tag", err)
			return
		}
		rootDigest = d
	}

	platform := oci.Platform{
		OS:           req.Platform.OS,
		Architecture: req.Platform.Architecture,
		Variant:      req.Platform.Variant,
	}
	record := func(status, selected, errMsg string, chain []store.ChainEntry) *store.Resolution {
		res := &store.Resolution{
			RequestedRef:   req.Ref,
			RootDigest:     rootDigest,
			OS:             platform.OS,
			Architecture:   platform.Architecture,
			Variant:        platform.Variant,
			Status:         status,
			SelectedDigest: selected,
			Error:          errMsg,
			Chain:          chain,
		}
		id, err := s.st.CreateResolution(res)
		if err == nil {
			res.ID = id
		}
		return res
	}

	descs, err := s.st.IndexManifests(rootDigest)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "store_failed", err)
		return
	}
	sel, err := oci.Select(descs, platform)
	if err != nil {
		var amb *oci.AmbiguousError
		switch {
		case errors.As(err, &amb):
			res := record("ambiguous", "", err.Error(), nil)
			writeJSON(w, http.StatusConflict, map[string]any{
				"error":      apiError{Code: "ambiguous", Message: err.Error()},
				"resolution": res,
			})
		case errors.Is(err, oci.ErrNoMatch):
			res := record("no_match", "", err.Error(), nil)
			writeJSON(w, http.StatusNotFound, map[string]any{
				"error":      apiError{Code: "no_match", Message: err.Error()},
				"resolution": res,
			})
		default:
			writeErr(w, http.StatusInternalServerError, "select_failed", err)
		}
		return
	}

	chain, err := s.st.Chain(rootDigest, sel.Digest)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "chain_failed", err)
		return
	}
	res := record("resolved", sel.Digest, "", chain)
	writeJSON(w, http.StatusOK, res)
}

// ---- queries ----

func (s *Server) handleGetResolution(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_id", err)
		return
	}
	res, err := s.st.GetResolution(id)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "store_failed", err)
		return
	}
	if res == nil {
		writeErr(w, http.StatusNotFound, "not_found", fmt.Errorf("resolution %d not found", id))
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handleListResolutions(w http.ResponseWriter, _ *http.Request) {
	list, err := s.st.ListResolutions()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "store_failed", err)
		return
	}
	writeJSON(w, http.StatusOK, list)
}

func (s *Server) handleListTags(w http.ResponseWriter, _ *http.Request) {
	tags, err := s.st.ListTags()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "store_failed", err)
		return
	}
	writeJSON(w, http.StatusOK, tags)
}

func (s *Server) handleTagHistory(w http.ResponseWriter, r *http.Request) {
	wild := chi.URLParam(r, "*")
	name, ok := strings.CutSuffix(wild, "/history")
	if !ok || name == "" {
		writeErr(w, http.StatusNotFound, "not_found",
			fmt.Errorf("use GET /api/v1/tags/<name>/history"))
		return
	}
	h, err := s.st.TagHistory(name)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "store_failed", err)
		return
	}
	writeJSON(w, http.StatusOK, h)
}
