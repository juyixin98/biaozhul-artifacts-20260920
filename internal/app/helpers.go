package app

import (
	"context"
	"errors"
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

	"community-governance/internal/database"
	"community-governance/internal/httpx"
)

// sentinel errors mapped to status codes by handler wrappers.
var (
	errForbidden    = errors.New("forbidden")
	errConflict     = errors.New("conflict")
	errBadRequest   = errors.New("bad request")
	errNotFound     = errors.New("not found")
	errSubscription = errors.New("no active subscription")
)

func unauthorized(w http.ResponseWriter, msg string) { httpx.Error(w, http.StatusUnauthorized, msg) }
func badRequest(w http.ResponseWriter, msg string)   { httpx.Error(w, http.StatusBadRequest, msg) }
func forbidden(w http.ResponseWriter, msg string)    { httpx.Error(w, http.StatusForbidden, msg) }
func notFound(w http.ResponseWriter, msg string)     { httpx.Error(w, http.StatusNotFound, msg) }
func conflict(w http.ResponseWriter, msg string)     { httpx.Error(w, http.StatusConflict, msg) }
func serverError(w http.ResponseWriter, e error) {
	httpx.Error(w, http.StatusInternalServerError, e.Error())
}

// writeErr maps domain errors to HTTP status codes.
func writeErr(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, errBadRequest):
		badRequest(w, err.Error())
	case errors.Is(err, errForbidden):
		forbidden(w, err.Error())
	case errors.Is(err, errConflict):
		conflict(w, err.Error())
	case errors.Is(err, errNotFound):
		notFound(w, err.Error())
	case errors.Is(err, pgx.ErrNoRows):
		notFound(w, "resource not found")
	default:
		serverError(w, err)
	}
}

func urlID(r *http.Request, key string) (int64, error) {
	v := chi.URLParam(r, key)
	id, err := strconv.ParseInt(v, 10, 64)
	if err != nil || id <= 0 {
		return 0, errBadRequest
	}
	return id, nil
}

// loadCommunityOr404 verifies the community exists and the caller belongs to it.
func (a *App) loadCommunityOr404(w http.ResponseWriter, r *http.Request) (int64, bool) {
	cid, err := urlID(r, "cid")
	if err != nil {
		writeErr(w, err)
		return 0, false
	}
	u := currentUser(r)
	if cid != u.CommunityID {
		notFound(w, "community not found")
		return 0, false
	}
	if _, err := a.Q.GetCommunity(r.Context(), cid); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			notFound(w, "community not found")
		} else {
			serverError(w, err)
		}
		return 0, false
	}
	return cid, true
}

// requireAdmin is a plain helper used inside handlers (routes already authed).
func requireAdmin(w http.ResponseWriter, u CurrentUser) bool {
	if !u.isAdmin() {
		forbidden(w, "admin role required")
		return false
	}
	return true
}

func requireModerator(w http.ResponseWriter, u CurrentUser) bool {
	if !u.isModerator() {
		forbidden(w, "moderator role required")
		return false
	}
	return true
}

// readableResult is what the single authorization choke-point returns.
type readableResult struct {
	Content database.Content
	Version database.ContentVersion
	// Bypassed is true for author/moderator reads (not member gated reads).
	Bypassed bool
}

// resolveReadableVersion is THE authorization function used by body reads,
// attachment downloads and export, so all three enforce identical rules.
//
//	author of the content      -> any version of their own content
//	admin / moderator          -> any version (review workload)
//	any other member           -> content published, version == the frozen
//	                              published version, subscription active &
//	                              unexpired, effective tier level >= required
//
// wantVersion 0 means "the version appropriate for the caller": the published
// version for ordinary members.
func (a *App) resolveReadableVersion(
	ctx context.Context, q *database.Queries,
	u CurrentUser, contentID int64, wantVersion int64,
) (readableResult, error) {
	c, err := q.GetContent(ctx, database.GetContentParams{ID: contentID, CommunityID: u.CommunityID})
	if err != nil {
		return readableResult{}, err
	}

	bypass := u.isModerator() || c.AuthorID == u.ID
	if bypass {
		vid := wantVersion
		if vid == 0 {
			if u.isModerator() && c.PublishedVersionID.Valid {
				vid = c.PublishedVersionID.Int64
			} else {
				vid = c.CurrentVersionID.Int64
			}
		}
		v, err := q.GetVersion(ctx, database.GetVersionParams{ID: vid, CommunityID: u.CommunityID})
		if err != nil {
			return readableResult{}, err
		}
		return readableResult{Content: c, Version: v, Bypassed: true}, nil
	}

	// Member path: published, frozen version only.
	if c.Status != "published" || !c.PublishedVersionID.Valid {
		if c.Status == "delisted" {
			return readableResult{}, errors.New("content has been delisted")
		}
		return readableResult{}, errForbidden
	}
	pubID := c.PublishedVersionID.Int64
	if wantVersion != 0 && wantVersion != pubID {
		// Asking for a draft/other version as a non-privileged member.
		return readableResult{}, errForbidden
	}

	level, err := q.GetEffectiveLevel(ctx, database.GetEffectiveLevelParams{
		CommunityID: u.CommunityID, UserID: u.ID,
	})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return readableResult{}, errors.New("active membership required")
		}
		return readableResult{}, err
	}
	if level < c.RequiredLevel {
		return readableResult{}, errors.New("your membership tier does not include this content")
	}

	v, err := q.GetVersion(ctx, database.GetVersionParams{ID: pubID, CommunityID: u.CommunityID})
	if err != nil {
		return readableResult{}, err
	}
	return readableResult{Content: c, Version: v}, nil
}
