package app

import (
	"errors"
	"fmt"
	"net/http"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"community-governance/internal/database"
	"community-governance/internal/httpx"
)

// createContent starts a draft and its first immutable version.
func (a *App) createContent(w http.ResponseWriter, r *http.Request) {
	cid, ok := a.loadCommunityOr404(w, r)
	if !ok {
		return
	}
	u := currentUser(r)
	var req struct {
		Title         string `json:"title"`
		Body          string `json:"body"`
		RequiredLevel int32  `json:"required_level"`
	}
	if err := httpx.Decode(r, &req); err != nil || req.Title == "" {
		badRequest(w, "title required")
		return
	}
	if req.RequiredLevel == 0 {
		req.RequiredLevel = 1
	}
	if req.RequiredLevel < 1 || req.RequiredLevel > 10 {
		badRequest(w, "required_level must be between 1 and 10")
		return
	}

	var c database.Content
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		var err error
		c, err = q.CreateContent(r.Context(), database.CreateContentParams{
			CommunityID: cid, AuthorID: u.ID, Title: req.Title,
			RequiredLevel: req.RequiredLevel,
		})
		if err != nil {
			return err
		}
		v, err := q.CreateVersion(r.Context(), database.CreateVersionParams{
			ContentID: c.ID, CommunityID: cid, VersionNo: 1,
			Body: req.Body, CreatedBy: u.ID,
		})
		if err != nil {
			return err
		}
		// Bind current pointer; published only on approval.
		if err := q.BindInitialVersion(r.Context(), database.BindInitialVersionParams{
			ID: c.ID, CommunityID: cid,
			CurrentVersionID: pgtype.Int8{Int64: v.ID, Valid: true},
		}); err != nil {
			return err
		}
		return q.InsertContentEvent(r.Context(), database.InsertContentEventParams{
			CommunityID: cid, ContentID: c.ID,
			VersionID: pgtype.Int8{Int64: v.ID, Valid: true},
			ActorID:   u.ID, Action: "submit", Reason: "draft created",
		})
	})
	if txErr != nil {
		writeErr(w, txErr)
		return
	}
	httpx.JSON(w, http.StatusCreated, c)
}

// createVersion appends an immutable edit. Any member who is the author may
// edit; while the content was pending review it is reset to draft so the new
// version cannot be auto-published by an in-flight approval.
func (a *App) createVersion(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	var req struct {
		Title string `json:"title"`
		Body  string `json:"body"`
	}
	if err := httpx.Decode(r, &req); err != nil || req.Body == "" && req.Title == "" {
		badRequest(w, "body or title required")
		return
	}

	var c database.Content
	var v database.ContentVersion
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		var e error
		// Lock content row: serialize against approve/restore.
		c, e = q.GetContentForUpdate(r.Context(),
			database.GetContentForUpdateParams{ID: id, CommunityID: u.CommunityID})
		if e != nil {
			return e
		}
		if c.AuthorID != u.ID {
			return errForbidden
		}
		next, e := q.NextVersionNo(r.Context(), c.ID)
		if e != nil {
			return e
		}
		v, e = q.CreateVersion(r.Context(), database.CreateVersionParams{
			ContentID: c.ID, CommunityID: u.CommunityID,
			VersionNo: next, Body: req.Body, CreatedBy: u.ID,
		})
		if e != nil {
			return e
		}
		title := req.Title
		if title == "" {
			title = c.Title
		}
		c, e = q.EditContentSwapVersion(r.Context(), database.EditContentSwapVersionParams{
			ID: c.ID, CommunityID: u.CommunityID,
			NewVersionID: pgtype.Int8{Int64: v.ID, Valid: true},
			Title:        title,
			OldVersionID: pgtype.Int8{Int64: c.CurrentVersionID.Int64, Valid: true},
		})
		if e != nil {
			return e
		}
		return q.InsertContentEvent(r.Context(), database.InsertContentEventParams{
			CommunityID: u.CommunityID, ContentID: c.ID,
			VersionID: pgtype.Int8{Int64: v.ID, Valid: true},
			ActorID:   u.ID, Action: "submit",
			Reason: fmt.Sprintf("new version %d", next),
		})
	})
	if txErr != nil {
		writeErr(w, txErr)
		return
	}
	httpx.JSON(w, http.StatusCreated, map[string]any{"content": c, "version": v})
}

func (a *App) listContents(w http.ResponseWriter, r *http.Request) {
	cid, ok := a.loadCommunityOr404(w, r)
	if !ok {
		return
	}
	u := currentUser(r)
	status := r.URL.Query().Get("status")
	if status != "" && status != "draft" && status != "pending" &&
		status != "published" && status != "delisted" {
		badRequest(w, "invalid status")
		return
	}
	rows, err := a.Q.ListContents(r.Context(),
		database.ListContentsParams{CommunityID: cid, Status: pgText(status)})
	if err != nil {
		serverError(w, err)
		return
	}
	out := make([]database.Content, 0, len(rows))
	for _, c := range rows {
		// Members only ever see published content they can access; moderators
		// see everything; authors additionally see their own drafts in detail.
		if u.isModerator() {
			out = append(out, c)
			continue
		}
		if c.Status == "published" {
			if c.AuthorID == u.ID {
				out = append(out, c)
				continue
			}
			level, err := a.Q.GetEffectiveLevel(r.Context(), database.GetEffectiveLevelParams{
				CommunityID: cid, UserID: u.ID,
			})
			if err == nil && level >= c.RequiredLevel {
				out = append(out, c)
			}
		} else if c.AuthorID == u.ID {
			out = append(out, c)
		}
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"contents": out})
}

func (a *App) getContent(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	c, err := a.Q.GetContent(r.Context(),
		database.GetContentParams{ID: id, CommunityID: u.CommunityID})
	if err != nil {
		writeErr(w, err)
		return
	}
	if !u.isModerator() && c.AuthorID != u.ID {
		// Members only see metadata for accessible published content.
		res, err := a.resolveReadableVersion(r.Context(), a.Q, u, id, 0)
		if err != nil {
			forbidden(w, err.Error())
			return
		}
		httpx.JSON(w, http.StatusOK, map[string]any{"content": c, "version": res.Version})
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"content": c})
}

// submitContent sends the *bound* version for review. Body carries version_id
// so the request explicitly identifies what is being submitted.
func (a *App) submitContent(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	var req struct {
		VersionID int64 `json:"version_id"`
	}
	if err := httpx.Decode(r, &req); err != nil || req.VersionID <= 0 {
		badRequest(w, "version_id required")
		return
	}

	var c database.Content
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		var e error
		c, e = q.GetContentForUpdate(r.Context(),
			database.GetContentForUpdateParams{ID: id, CommunityID: u.CommunityID})
		if e != nil {
			return e
		}
		if c.AuthorID != u.ID {
			return errForbidden
		}
		if req.VersionID != c.CurrentVersionID.Int64 {
			return errBadRequest
		}
		c, e = q.SubmitContent(r.Context(), database.SubmitContentParams{
			ID: id, CommunityID: u.CommunityID,
			CurrentVersionID: pgtype.Int8{Int64: req.VersionID, Valid: true},
		})
		if e != nil {
			if errors.Is(e, pgx.ErrNoRows) {
				return errConflict
			}
			return e
		}
		if e := q.MarkVersionPending(r.Context(), database.MarkVersionPendingParams{
			ID: req.VersionID, CommunityID: u.CommunityID,
		}); e != nil {
			return e
		}
		return q.InsertContentEvent(r.Context(), database.InsertContentEventParams{
			CommunityID: u.CommunityID, ContentID: id,
			VersionID: pgtype.Int8{Int64: req.VersionID, Valid: true},
			ActorID:   u.ID, Action: "submit", Reason: "submitted for review",
		})
	})
	if txErr != nil {
		writeErr(w, txErr)
		return
	}
	httpx.JSON(w, http.StatusOK, c)
}

// approveContent binds both the expected content status ('pending') and the
// exact reviewed version. An edit made concurrently moves current_version_id
// forward; the UPDATE then matches zero rows and returns 409 instead of
// publishing unreviewed content.
func (a *App) approveContent(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	if !requireModerator(w, u) {
		return
	}
	var req struct {
		VersionID int64  `json:"version_id"`
		Reason    string `json:"reason"`
	}
	if err := httpx.Decode(r, &req); err != nil || req.VersionID <= 0 {
		badRequest(w, "version_id required")
		return
	}

	var c database.Content
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		var e error
		// Author cannot self-approve.
		c, e = q.GetContentForUpdate(r.Context(),
			database.GetContentForUpdateParams{ID: id, CommunityID: u.CommunityID})
		if e != nil {
			return e
		}
		if c.AuthorID == u.ID {
			return errForbidden // author cannot self-review
		}
		c, e = q.ApproveContent(r.Context(), database.ApproveContentParams{
			ID: id, CommunityID: u.CommunityID,
			CurrentVersionID: pgtype.Int8{Int64: req.VersionID, Valid: true},
		})
		if e != nil {
			if errors.Is(e, pgx.ErrNoRows) {
				return errConflict
			}
			return e
		}
		if e := q.ApproveVersion(r.Context(), database.ApproveVersionParams{
			ID: req.VersionID, CommunityID: u.CommunityID,
		}); e != nil {
			return e
		}
		return q.InsertContentEvent(r.Context(), database.InsertContentEventParams{
			CommunityID: u.CommunityID, ContentID: id,
			VersionID: pgtype.Int8{Int64: req.VersionID, Valid: true},
			ActorID:   u.ID, Action: "approve", Reason: req.Reason,
		})
	})
	if txErr != nil {
		writeErr(w, txErr)
		return
	}
	httpx.JSON(w, http.StatusOK, c)
}

func (a *App) rejectContent(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	if !requireModerator(w, u) {
		return
	}
	var req struct {
		VersionID int64  `json:"version_id"`
		Reason    string `json:"reason"`
	}
	if err := httpx.Decode(r, &req); err != nil || req.VersionID <= 0 || req.Reason == "" {
		badRequest(w, "version_id and reason required")
		return
	}

	var c database.Content
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		var e error
		c, e = q.GetContentForUpdate(r.Context(),
			database.GetContentForUpdateParams{ID: id, CommunityID: u.CommunityID})
		if e != nil {
			return e
		}
		if c.AuthorID == u.ID {
			return errForbidden // author cannot self-review
		}
		c, e = q.RejectContent(r.Context(), database.RejectContentParams{
			ID: id, CommunityID: u.CommunityID,
			CurrentVersionID: pgtype.Int8{Int64: req.VersionID, Valid: true},
		})
		if e != nil {
			if errors.Is(e, pgx.ErrNoRows) {
				return errConflict
			}
			return e
		}
		if e := q.RejectVersion(r.Context(), database.RejectVersionParams{
			ID: req.VersionID, CommunityID: u.CommunityID,
		}); e != nil {
			return e
		}
		return q.InsertContentEvent(r.Context(), database.InsertContentEventParams{
			CommunityID: u.CommunityID, ContentID: id,
			VersionID: pgtype.Int8{Int64: req.VersionID, Valid: true},
			ActorID:   u.ID, Action: "reject", Reason: req.Reason,
		})
	})
	if txErr != nil {
		writeErr(w, txErr)
		return
	}
	httpx.JSON(w, http.StatusOK, c)
}

func (a *App) delistContent(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	if !requireModerator(w, u) {
		return
	}
	var req struct {
		Reason string `json:"reason"`
	}
	_ = httpx.Decode(r, &req)

	var c database.Content
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		var e error
		c, e = q.GetContentForUpdate(r.Context(),
			database.GetContentForUpdateParams{ID: id, CommunityID: u.CommunityID})
		if e != nil {
			return e
		}
		c, e = q.DelistContent(r.Context(), database.DelistContentParams{
			ID: id, CommunityID: u.CommunityID,
		})
		if e != nil {
			if errors.Is(e, pgx.ErrNoRows) {
				return errConflict
			}
			return e
		}
		return q.InsertContentEvent(r.Context(), database.InsertContentEventParams{
			CommunityID: u.CommunityID, ContentID: id,
			VersionID: c.PublishedVersionID,
			ActorID:   u.ID, Action: "delist", Reason: req.Reason,
		})
	})
	if txErr != nil {
		writeErr(w, txErr)
		return
	}
	httpx.JSON(w, http.StatusOK, c)
}

// restoreContent is bound to the exact version to restore AND the requirement
// that it is still the latest version; a newer edit after the takedown makes
// the guarded UPDATE match zero rows.
func (a *App) restoreContent(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	if !requireModerator(w, u) {
		return
	}
	var req struct {
		VersionID int64  `json:"version_id"`
		Reason    string `json:"reason"`
	}
	if err := httpx.Decode(r, &req); err != nil || req.VersionID <= 0 || req.Reason == "" {
		badRequest(w, "version_id and reason required")
		return
	}

	var c database.Content
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		var e error
		c, e = q.GetContentForUpdate(r.Context(),
			database.GetContentForUpdateParams{ID: id, CommunityID: u.CommunityID})
		if e != nil {
			return e
		}
		c, e = q.RestoreContent(r.Context(), database.RestoreContentParams{
			ID: id, CommunityID: u.CommunityID,
			PublishedVersionID: pgtype.Int8{Int64: req.VersionID, Valid: true},
		})
		if e != nil {
			if errors.Is(e, pgx.ErrNoRows) {
				return errConflict
			}
			return e
		}
		return q.InsertContentEvent(r.Context(), database.InsertContentEventParams{
			CommunityID: u.CommunityID, ContentID: id,
			VersionID: pgtype.Int8{Int64: req.VersionID, Valid: true},
			ActorID:   u.ID, Action: "restore", Reason: req.Reason,
		})
	})
	if txErr != nil {
		writeErr(w, txErr)
		return
	}
	httpx.JSON(w, http.StatusOK, c)
}

func (a *App) listVersions(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	c, err := a.Q.GetContent(r.Context(),
		database.GetContentParams{ID: id, CommunityID: u.CommunityID})
	if err != nil {
		writeErr(w, err)
		return
	}
	// Version history: author and moderators see all versions; members see the
	// single published frozen version (via same choke-point).
	if !u.isModerator() && c.AuthorID != u.ID {
		res, err := a.resolveReadableVersion(r.Context(), a.Q, u, id, 0)
		if err != nil {
			forbidden(w, err.Error())
			return
		}
		httpx.JSON(w, http.StatusOK, map[string]any{"versions": []database.ContentVersion{res.Version}})
		return
	}
	vs, err := a.Q.ListVersions(r.Context(),
		database.ListVersionsParams{ContentID: id, CommunityID: u.CommunityID})
	if err != nil {
		serverError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"versions": vs})
}

// getVersion returns a specific version body after the same authorization
// check used for downloads and export.
func (a *App) getVersion(w http.ResponseWriter, r *http.Request) {
	vid, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	v, err := a.Q.GetVersion(r.Context(),
		database.GetVersionParams{ID: vid, CommunityID: u.CommunityID})
	if err != nil {
		writeErr(w, err)
		return
	}
	res, err := a.resolveReadableVersion(r.Context(), a.Q, u, v.ContentID, vid)
	if err != nil {
		forbidden(w, err.Error())
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{
		"content": res.Content, "version": res.Version,
	})
}

func (a *App) listContentEvents(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	c, err := a.Q.GetContent(r.Context(),
		database.GetContentParams{ID: id, CommunityID: u.CommunityID})
	if err != nil {
		writeErr(w, err)
		return
	}
	// Audit trail visible to moderators and the author.
	if !u.isModerator() && c.AuthorID != u.ID {
		forbidden(w, "audit trail is restricted to moderators and the author")
		return
	}
	evs, err := a.Q.ListContentEvents(r.Context(),
		database.ListContentEventsParams{ContentID: id, CommunityID: u.CommunityID})
	if err != nil {
		serverError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"events": evs})
}

func pgText(s string) pgtype.Text {
	if s == "" {
		return pgtype.Text{Valid: false}
	}
	return pgtype.Text{String: s, Valid: true}
}
