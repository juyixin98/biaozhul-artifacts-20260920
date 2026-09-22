package handlers

import (
	"net/http"

	"communitygov/internal/services"
)

type fileReportReq struct {
	PostID          int64  `json:"post_id"`
	TargetVersionID int64  `json:"target_version_id"`
	Category        string `json:"category"`
	Reason          string `json:"reason"`
}

func (h *Handlers) FileReport(w http.ResponseWriter, r *http.Request) {
	var req fileReportReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return
	}
	cid := urlInt64(r, "communityID")
	rep, err := h.Reports.File(r.Context(), cid, currentUser(r).ID,
		req.PostID, req.TargetVersionID, req.Category, req.Reason)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, rep)
}

type decisionReq struct {
	Reason string `json:"reason"`
}

func (h *Handlers) moderate(w http.ResponseWriter, r *http.Request) (int64, string, bool) {
	var req decisionReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return 0, "", false
	}
	cid := urlInt64(r, "communityID")
	u := currentUser(r)
	if u.Role != "admin" && !h.Users.IsReviewer(r.Context(), cid, u.ID) {
		writeErr(w, services.E(services.ErrForbidden, "not a reviewer of this community"))
		return 0, "", false
	}
	return cid, req.Reason, true
}

func (h *Handlers) AcceptReport(w http.ResponseWriter, r *http.Request) {
	cid, reason, ok := h.moderate(w, r)
	if !ok {
		return
	}
	rep, err := h.Reports.Accept(r.Context(), urlInt64(r, "reportID"), cid, currentUser(r).ID, reason)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

func (h *Handlers) RejectReport(w http.ResponseWriter, r *http.Request) {
	cid, reason, ok := h.moderate(w, r)
	if !ok {
		return
	}
	rep, err := h.Reports.Reject(r.Context(), urlInt64(r, "reportID"), cid, currentUser(r).ID, reason)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

func (h *Handlers) UpholdReport(w http.ResponseWriter, r *http.Request) {
	cid, reason, ok := h.moderate(w, r)
	if !ok {
		return
	}
	rep, err := h.Reports.Uphold(r.Context(), urlInt64(r, "reportID"), cid, currentUser(r).ID, reason)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

func (h *Handlers) OverturnReport(w http.ResponseWriter, r *http.Request) {
	cid, reason, ok := h.moderate(w, r)
	if !ok {
		return
	}
	rep, err := h.Reports.Overturn(r.Context(), urlInt64(r, "reportID"), cid, currentUser(r).ID, reason)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

type appealReq struct {
	Reason string `json:"reason"`
}

// AppealReport is a member-facing endpoint (the author or original reporter),
// not a moderator action.
func (h *Handlers) AppealReport(w http.ResponseWriter, r *http.Request) {
	var req appealReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return
	}
	cid := urlInt64(r, "communityID")
	rep, err := h.Reports.Appeal(r.Context(), urlInt64(r, "reportID"), cid,
		currentUser(r).ID, req.Reason)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

type restoreReportReq struct {
	VersionID int64  `json:"version_id"`
	Reason    string `json:"reason"`
}

func (h *Handlers) RestoreFromReport(w http.ResponseWriter, r *http.Request) {
	var req restoreReportReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return
	}
	cid := urlInt64(r, "communityID")
	u := currentUser(r)
	if u.Role != "admin" && !h.Users.IsReviewer(r.Context(), cid, u.ID) {
		writeErr(w, services.E(services.ErrForbidden, "not a reviewer of this community"))
		return
	}
	rep, post, err := h.Reports.Restore(r.Context(), urlInt64(r, "reportID"),
		cid, u.ID, req.VersionID, req.Reason)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"report": rep, "post": post})
}

func (h *Handlers) GetReport(w http.ResponseWriter, r *http.Request) {
	rep, err := h.Reports.Get(r.Context(), urlInt64(r, "reportID"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

func (h *Handlers) ListReports(w http.ResponseWriter, r *http.Request) {
	cid := urlInt64(r, "communityID")
	status := r.URL.Query().Get("status")
	rep, err := h.Reports.List(r.Context(), cid, status,
		queryInt32(r, "limit", 50), queryInt32(r, "offset", 0))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

func (h *Handlers) ReportDecisions(w http.ResponseWriter, r *http.Request) {
	ds, err := h.Reports.Decisions(r.Context(), urlInt64(r, "reportID"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, ds)
}
