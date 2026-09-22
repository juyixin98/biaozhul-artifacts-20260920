package app

import (
	"errors"
	"net/http"
	"strconv"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"community-governance/internal/database"
	"community-governance/internal/httpx"
)

// createReport targets a specific immutable version. Any member may report.
func (a *App) createReport(w http.ResponseWriter, r *http.Request) {
	contentID, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	var req struct {
		VersionID int64  `json:"version_id"`
		Category  string `json:"category"`
		Reason    string `json:"reason"`
	}
	if err := httpx.Decode(r, &req); err != nil ||
		req.VersionID <= 0 || req.Category == "" || req.Reason == "" {
		badRequest(w, "version_id, category and reason required")
		return
	}

	var rep database.Report
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		c, err := q.GetContent(r.Context(),
			database.GetContentParams{ID: contentID, CommunityID: u.CommunityID})
		if err != nil {
			return err
		}
		v, err := q.GetVersion(r.Context(),
			database.GetVersionParams{ID: req.VersionID, CommunityID: u.CommunityID})
		if err != nil {
			return err
		}
		if v.ContentID != c.ID {
			return errBadRequest
		}
		rep, err = q.CreateReport(r.Context(), database.CreateReportParams{
			CommunityID: u.CommunityID, ContentID: contentID, VersionID: req.VersionID,
			ReporterID: u.ID, Category: req.Category, Reason: req.Reason,
		})
		if err != nil {
			return err
		}
		return q.InsertReportEvent(r.Context(), database.InsertReportEventParams{
			ReportID: rep.ID, CommunityID: u.CommunityID, ActorID: u.ID,
			Action: "create", Basis: req.Category + ": " + req.Reason,
		})
	})
	if txErr != nil {
		writeErr(w, txErr)
		return
	}
	httpx.JSON(w, http.StatusCreated, rep)
}

func (a *App) listReports(w http.ResponseWriter, r *http.Request) {
	cid, ok := a.loadCommunityOr404(w, r)
	if !ok {
		return
	}
	u := currentUser(r)
	// Moderators see the queue. Authors see reports about their own contents.
	status := pgText(r.URL.Query().Get("status"))
	rows, err := a.Q.ListReports(r.Context(),
		database.ListReportsParams{CommunityID: cid, Status: status})
	if err != nil {
		serverError(w, err)
		return
	}
	out := make([]database.Report, 0, len(rows))
	for _, rp := range rows {
		if u.isModerator() {
			out = append(out, rp)
			continue
		}
		c, err := a.Q.GetContent(r.Context(),
			database.GetContentParams{ID: rp.ContentID, CommunityID: cid})
		if err == nil && c.AuthorID == u.ID {
			out = append(out, rp)
		}
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"reports": out})
}

func (a *App) loadReport(w http.ResponseWriter, r *http.Request, modOnly bool) (database.Report, CurrentUser, bool) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return database.Report{}, CurrentUser{}, false
	}
	u := currentUser(r)
	rp, err := a.Q.GetReport(r.Context(),
		database.GetReportParams{ID: id, CommunityID: u.CommunityID})
	if err != nil {
		writeErr(w, err)
		return database.Report{}, CurrentUser{}, false
	}
	if modOnly && !u.isModerator() {
		forbidden(w, "moderator role required")
		return database.Report{}, CurrentUser{}, false
	}
	return rp, u, true
}

func (a *App) getReport(w http.ResponseWriter, r *http.Request) {
	rp, u, ok := a.loadReport(w, r, false)
	if !ok {
		return
	}
	if !u.isModerator() {
		c, err := a.Q.GetContent(r.Context(),
			database.GetContentParams{ID: rp.ContentID, CommunityID: u.CommunityID})
		if err != nil || c.AuthorID != u.ID {
			if rp.ReporterID != u.ID {
				forbidden(w, "not your report")
				return
			}
		}
	}
	httpx.JSON(w, http.StatusOK, rp)
}

// acceptReport marks a report as taken up for review (受理). Idempotent while
// still in 'accepted' state; a decided report cannot be re-accepted.
func (a *App) acceptReport(w http.ResponseWriter, r *http.Request) {
	rp, u, ok := a.loadReport(w, r, true)
	if !ok {
		return
	}
	var req struct {
		Basis string `json:"basis"`
	}
	_ = httpx.Decode(r, &req)
	updated, err := a.Q.AcceptReport(r.Context(),
		database.AcceptReportParams{ID: rp.ID, CommunityID: u.CommunityID})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			conflict(w, "report already decided")
			return
		}
		serverError(w, err)
		return
	}
	if err := a.Q.InsertReportEvent(r.Context(), database.InsertReportEventParams{
		ReportID: rp.ID, CommunityID: u.CommunityID, ActorID: u.ID,
		Action: "accept", Basis: req.Basis,
	}); err != nil {
		serverError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, updated)
}

// upholdReport rules against the content (裁决违规): records the decision with
// actor + basis and immediately delists the reported version's content.
func (a *App) upholdReport(w http.ResponseWriter, r *http.Request) {
	rp, u, ok := a.loadReport(w, r, true)
	if !ok {
		return
	}
	var req struct {
		Basis string `json:"basis"`
	}
	if err := httpx.Decode(r, &req); err != nil || req.Basis == "" {
		badRequest(w, "basis required")
		return
	}

	var updated database.Report
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		rp2, err := q.GetReportForUpdate(r.Context(),
			database.GetReportForUpdateParams{ID: rp.ID, CommunityID: u.CommunityID})
		if err != nil {
			return err
		}
		if rp2.Appealed {
			updated, err = q.DecideAppealUpheld(r.Context(), database.DecideAppealUpheldParams{
				ID: rp.ID, CommunityID: u.CommunityID,
			})
		} else {
			updated, err = q.UpholdReport(r.Context(), database.UpholdReportParams{
				ID: rp.ID, CommunityID: u.CommunityID,
			})
		}
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return errConflict
			}
			return err
		}
		// Take the content down immediately, binding the reported version in
		// the content audit trail.
		c, err := q.DelistContent(r.Context(),
			database.DelistContentParams{ID: rp2.ContentID, CommunityID: u.CommunityID})
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		_ = c
		if err := q.InsertContentEvent(r.Context(), database.InsertContentEventParams{
			CommunityID: u.CommunityID, ContentID: rp2.ContentID,
			VersionID: pgtype.Int8{Int64: rp2.VersionID, Valid: true},
			ActorID:   u.ID, Action: "delist",
			Reason: "report #" + strconv.FormatInt(rp2.ID, 10) + " upheld: " + req.Basis,
		}); err != nil {
			return err
		}
		action := "uphold"
		if rp2.Appealed {
			action = "appeal_uphold"
		}
		return q.InsertReportEvent(r.Context(), database.InsertReportEventParams{
			ReportID: rp.ID, CommunityID: u.CommunityID, ActorID: u.ID,
			Action: action, Basis: req.Basis,
		})
	})
	if txErr != nil {
		writeErr(w, txErr)
		return
	}
	httpx.JSON(w, http.StatusOK, updated)
}

// dismissReport rules in favor of the content (裁决不违规).
func (a *App) dismissReport(w http.ResponseWriter, r *http.Request) {
	rp, u, ok := a.loadReport(w, r, true)
	if !ok {
		return
	}
	var req struct {
		Basis string `json:"basis"`
	}
	if err := httpx.Decode(r, &req); err != nil || req.Basis == "" {
		badRequest(w, "basis required")
		return
	}

	var updated database.Report
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		rp2, err := q.GetReportForUpdate(r.Context(),
			database.GetReportForUpdateParams{ID: rp.ID, CommunityID: u.CommunityID})
		if err != nil {
			return err
		}
		if rp2.Appealed {
			updated, err = q.DecideAppealDismissed(r.Context(), database.DecideAppealDismissedParams{
				ID: rp.ID, CommunityID: u.CommunityID,
			})
		} else {
			updated, err = q.DismissReport(r.Context(), database.DismissReportParams{
				ID: rp.ID, CommunityID: u.CommunityID,
			})
		}
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return errConflict
			}
			return err
		}
		action := "dismiss"
		if rp2.Appealed {
			action = "appeal_dismiss"
		}
		return q.InsertReportEvent(r.Context(), database.InsertReportEventParams{
			ReportID: rp.ID, CommunityID: u.CommunityID, ActorID: u.ID,
			Action: action, Basis: req.Basis,
		})
	})
	if txErr != nil {
		writeErr(w, txErr)
		return
	}
	httpx.JSON(w, http.StatusOK, updated)
}

// appealReport lets the reported content's author appeal once.
func (a *App) appealReport(w http.ResponseWriter, r *http.Request) {
	rp, u, ok := a.loadReport(w, r, false)
	if !ok {
		return
	}
	c, err := a.Q.GetContent(r.Context(),
		database.GetContentParams{ID: rp.ContentID, CommunityID: u.CommunityID})
	if err != nil || c.AuthorID != u.ID {
		forbidden(w, "only the reported content's author may appeal")
		return
	}
	var req struct {
		Basis string `json:"basis"`
	}
	if err := httpx.Decode(r, &req); err != nil || req.Basis == "" {
		badRequest(w, "basis required")
		return
	}
	updated, err := a.Q.AppealReport(r.Context(),
		database.AppealReportParams{ID: rp.ID, CommunityID: u.CommunityID})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			conflict(w, "report cannot be appealed (already appealed or not decided)")
			return
		}
		serverError(w, err)
		return
	}
	if err := a.Q.InsertReportEvent(r.Context(), database.InsertReportEventParams{
		ReportID: rp.ID, CommunityID: u.CommunityID, ActorID: u.ID,
		Action: "appeal", Basis: req.Basis,
	}); err != nil {
		serverError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, updated)
}

// appealUphold / appealDismiss are convenience aliases that force the appeal
// branch even in racy ordering between appeal insertion and decision.
func (a *App) appealUphold(w http.ResponseWriter, r *http.Request)  { a.upholdReport(w, r) }
func (a *App) appealDismiss(w http.ResponseWriter, r *http.Request) { a.dismissReport(w, r) }

func (a *App) listReportEvents(w http.ResponseWriter, r *http.Request) {
	rp, u, ok := a.loadReport(w, r, false)
	if !ok {
		return
	}
	if !u.isModerator() && rp.ReporterID != u.ID {
		c, err := a.Q.GetContent(r.Context(),
			database.GetContentParams{ID: rp.ContentID, CommunityID: u.CommunityID})
		if err != nil || c.AuthorID != u.ID {
			forbidden(w, "not your report")
			return
		}
	}
	evs, err := a.Q.ListReportEvents(r.Context(),
		database.ListReportEventsParams{ReportID: rp.ID, CommunityID: u.CommunityID})
	if err != nil {
		serverError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"events": evs})
}
