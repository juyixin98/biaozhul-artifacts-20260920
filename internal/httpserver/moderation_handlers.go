package httpserver

import (
	"net/http"

	"communityvault/internal/apperr"
	"communityvault/internal/db"
)

type reportReq struct {
	Reason string `json:"reason"`
}

func (s *Server) handleReport(w http.ResponseWriter, r *http.Request) {
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
	var req reportReq
	if err := decode(r, &req); err != nil {
		writeErr(w, err)
		return
	}
	rep, err := s.mod.Report(r.Context(), v.User.ID, id, req.Reason)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 201, map[string]any{"report": rep})
}

func (s *Server) handleClaim(w http.ResponseWriter, r *http.Request) {
	v, err := requireRole(r, "moderator", "admin")
	if err != nil {
		writeErr(w, err)
		return
	}
	task, claim, err := s.mod.Claim(r.Context(), *v.User, v.ModeratorCategories)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 201, map[string]any{
		"task":       task,
		"claim":      claim,
		"expires_at": claim.ExpiresAt.Time,
	})
}

func (s *Server) handleApprove(w http.ResponseWriter, r *http.Request) {
	v, err := requireRole(r, "moderator", "admin")
	if err != nil {
		writeErr(w, err)
		return
	}
	taskID, err := idParam(r, "taskID")
	if err != nil {
		writeErr(w, err)
		return
	}
	review, err := s.mod.Approve(r.Context(), *v.User, taskID, v.ModeratorCategories)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"review": review})
}

func (s *Server) handleReject(w http.ResponseWriter, r *http.Request) {
	v, err := requireRole(r, "moderator", "admin")
	if err != nil {
		writeErr(w, err)
		return
	}
	taskID, err := idParam(r, "taskID")
	if err != nil {
		writeErr(w, err)
		return
	}
	var req reasonReq
	if err := decode(r, &req); err != nil {
		writeErr(w, err)
		return
	}
	review, err := s.mod.Reject(r.Context(), *v.User, taskID, v.ModeratorCategories, req.Reason)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"review": review})
}

func (s *Server) handleRecycle(w http.ResponseWriter, r *http.Request) {
	if _, err := requireRole(r, "admin"); err != nil {
		writeErr(w, err)
		return
	}
	n, err := s.mod.Recycle(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"recycled": n})
}

func (s *Server) handleAllReports(w http.ResponseWriter, r *http.Request) {
	v, err := requireRole(r, "moderator", "admin")
	if err != nil {
		writeErr(w, err)
		return
	}
	limit := int32(50)
	var reports []db.Report
	if v.User.Role == "admin" {
		reports, err = db.New(s.pool).ListAllReports(r.Context(), limit)
	} else {
		if len(v.ModeratorCategories) == 0 {
			reports = []db.Report{}
		} else {
			reports, err = db.New(s.pool).ListReportsInCategories(r.Context(),
				db.ListReportsInCategoriesParams{Column1: v.ModeratorCategories, Limit: limit})
		}
	}
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"reports": reports})
}

func errBadID(name string) *apperr.Error {
	return apperr.New(400, "bad_id", name+" must be an integer")
}
