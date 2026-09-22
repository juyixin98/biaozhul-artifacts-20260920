package httpserver

import (
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5"

	"communityvault/internal/db"
	"communityvault/internal/view"
)

func toViewer(v Viewer) view.Viewer {
	return view.Viewer{User: v.User, ModeratorCategories: v.ModeratorCategories}
}

func idParam(r *http.Request, name string) (int64, error) {
	id, err := strconv.ParseInt(chi.URLParam(r, name), 10, 64)
	if err != nil {
		return 0, errBadID(name)
	}
	return id, nil
}

func (s *Server) handleFeed(w http.ResponseWriter, r *http.Request) {
	cursor, _ := strconv.ParseInt(r.URL.Query().Get("cursor"), 10, 64)
	limit, _ := strconv.ParseInt(r.URL.Query().Get("limit"), 10, 32)
	page, err := s.views.Feed(r.Context(), cursor, int32(limit))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, page)
}

type createContentReq struct {
	CategoryID int64  `json:"category_id"`
	Title      string `json:"title"`
	Body       string `json:"body"`
}

func (s *Server) handleCreateContent(w http.ResponseWriter, r *http.Request) {
	v, err := requireUser(r)
	if err != nil {
		writeErr(w, err)
		return
	}
	var req createContentReq
	if err := decode(r, &req); err != nil {
		writeErr(w, err)
		return
	}
	c, rev, err := s.cts.Create(r.Context(), v.User.ID, req.CategoryID, req.Title, req.Body)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 201, map[string]any{"content": c, "revision": rev})
}

func (s *Server) handleGetContent(w http.ResponseWriter, r *http.Request) {
	v := viewerFrom(r.Context())
	id, err := idParam(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	cv, err := s.views.GetContent(r.Context(), toViewer(v), id)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, cv)
}

type editReq struct {
	Body   string `json:"body"`
	Reason string `json:"reason"`
}

func (s *Server) handleEditContent(w http.ResponseWriter, r *http.Request) {
	v, err := requireUser(r)
	if err != nil {
		writeErr(w, err)
		return
	}
	id, err := idParam(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	var req editReq
	if err := decode(r, &req); err != nil {
		writeErr(w, err)
		return
	}
	rev, err := s.cts.Edit(r.Context(), v.User.ID, id, req.Body, req.Reason)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"revision": rev})
}

func (s *Server) handleSubmit(w http.ResponseWriter, r *http.Request) {
	v, err := requireUser(r)
	if err != nil {
		writeErr(w, err)
		return
	}
	id, err := idParam(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	c, matched, rv, err := s.cts.Submit(r.Context(), v.User.ID, id)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{
		"content": c,
		"rule":    ruleSummary(rv),
		"matched": matched,
	})
}

type reasonReq struct {
	Reason string `json:"reason"`
}

func (s *Server) handleWithdraw(w http.ResponseWriter, r *http.Request) {
	v, err := requireUser(r)
	if err != nil {
		writeErr(w, err)
		return
	}
	id, err := idParam(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	var req reasonReq
	if err := decode(r, &req); err != nil {
		writeErr(w, err)
		return
	}
	c, err := s.cts.Withdraw(r.Context(), *v.User, id, req.Reason, v.ModeratorCategories)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"content": c})
}

type rollbackReq struct {
	RevisionNo int32  `json:"revision_no"`
	Reason     string `json:"reason"`
}

func (s *Server) handleRollback(w http.ResponseWriter, r *http.Request) {
	v, err := requireUser(r)
	if err != nil {
		writeErr(w, err)
		return
	}
	id, err := idParam(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	var req rollbackReq
	if err := decode(r, &req); err != nil {
		writeErr(w, err)
		return
	}
	rev, c, err := s.cts.Rollback(r.Context(), v.User.ID, id, req.RevisionNo, req.Reason)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"content": c, "revision": rev})
}

func (s *Server) handleListRevisions(w http.ResponseWriter, r *http.Request) {
	v := viewerFrom(r.Context())
	id, err := idParam(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	revs, err := s.views.Revisions(r.Context(), toViewer(v), id)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"revisions": revs})
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	v := viewerFrom(r.Context())
	id, err := idParam(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	events, err := s.views.Events(r.Context(), toViewer(v), id)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"events": events})
}

func (s *Server) handleReviews(w http.ResponseWriter, r *http.Request) {
	v := viewerFrom(r.Context())
	id, err := idParam(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	reviews, err := s.views.Reviews(r.Context(), toViewer(v), id)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"reviews": reviews})
}

func (s *Server) handleListReports(w http.ResponseWriter, r *http.Request) {
	v := viewerFrom(r.Context())
	id, err := idParam(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	reports, err := s.views.ReportsForContent(r.Context(), toViewer(v), id)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"reports": reports})
}

func (s *Server) handleListCategories(w http.ResponseWriter, r *http.Request) {
	cats, err := db.New(s.pool).ListCategories(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"categories": cats})
}
