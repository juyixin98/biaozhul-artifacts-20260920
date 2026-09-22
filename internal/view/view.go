// Package view implements read-side visibility and role-based redaction.
//
// Visibility rules:
//   - anonymous / members: published content and its published revision only.
//   - the author: all of their own content's revisions, but audit trails are
//     anonymized (no reviewer identities, no reporter identities).
//   - moderators: content in their categories, full detail.
//   - admins: everything.
package view

import (
	"context"

	"github.com/jackc/pgx/v5/pgxpool"

	"communityvault/internal/apperr"
	"communityvault/internal/db"
)

type Service struct {
	pool *pgxpool.Pool
}

func New(pool *pgxpool.Pool) *Service { return &Service{pool: pool} }

// Viewer is the requesting principal; nil User means anonymous.
type Viewer struct {
	User                *db.User
	ModeratorCategories []int64
}

func (v Viewer) isAdmin() bool { return v.User != nil && v.User.Role == "admin" }

func (v Viewer) canSeeFull(c db.Content) bool {
	if v.User == nil {
		return false
	}
	if v.isAdmin() {
		return true
	}
	if c.AuthorID == v.User.ID {
		return true
	}
	if v.User.Role == "moderator" {
		for _, id := range v.ModeratorCategories {
			if id == c.CategoryID {
				return true
			}
		}
	}
	return false
}

func (v Viewer) isStaffFor(c db.Content) bool {
	if v.isAdmin() {
		return true
	}
	if v.User != nil && v.User.Role == "moderator" {
		for _, id := range v.ModeratorCategories {
			if id == c.CategoryID {
				return true
			}
		}
	}
	return false
}

// ContentView is the assembled content payload a viewer is allowed to see.
type ContentView struct {
	Content  db.Content          `json:"content"`
	Revision *db.ContentRevision `json:"revision"`
	Editable bool                `json:"editable"`
}

// GetContent returns content plus the single revision the viewer may read:
// the published revision for the public, the current (possibly draft)
// revision for the owner/staff.
func (s *Service) GetContent(ctx context.Context, v Viewer, id int64) (ContentView, error) {
	q := db.New(s.pool)
	c, err := q.GetContent(ctx, id)
	if err != nil {
		return ContentView{}, apperr.ErrNotFound
	}
	if c.Status == "published" {
		// Everyone reads the published revision, even if the author has a
		// newer draft pending: the new body is never leaked.
		rev, err := q.GetRevision(ctx, *c.PublishedRevision)
		if err != nil {
			return ContentView{}, err
		}
		return ContentView{Content: c, Revision: &rev, Editable: v.User != nil && c.AuthorID == v.User.ID}, nil
	}
	if !v.canSeeFull(c) {
		return ContentView{}, apperr.ErrNotFound
	}
	rev, err := q.GetRevision(ctx, *c.CurrentRevision)
	if err != nil {
		return ContentView{}, err
	}
	return ContentView{Content: c, Revision: &rev, Editable: c.AuthorID == v.User.ID && c.Status == "draft"}, nil
}

// Revisions lists revisions the viewer may see: full history for the author
// and staff; only the published revision for everyone else.
func (s *Service) Revisions(ctx context.Context, v Viewer, contentID int64) ([]db.ContentRevision, error) {
	q := db.New(s.pool)
	c, err := q.GetContent(ctx, contentID)
	if err != nil {
		return nil, apperr.ErrNotFound
	}
	if !v.canSeeFull(c) {
		if c.Status != "published" {
			return nil, apperr.ErrNotFound
		}
		rev, err := q.GetRevision(ctx, *c.PublishedRevision)
		if err != nil {
			return nil, err
		}
		return []db.ContentRevision{rev}, nil
	}
	return q.ListRevisions(ctx, contentID)
}

// EventView redacts the actor for non-staff viewers (authors see "what",
// never "which moderator").
type EventView struct {
	db.StatusEvent
	ActorID *int64 `json:"actor_id"`
}

func (s *Service) Events(ctx context.Context, v Viewer, contentID int64) ([]EventView, error) {
	q := db.New(s.pool)
	c, err := q.GetContent(ctx, contentID)
	if err != nil {
		return nil, apperr.ErrNotFound
	}
	if v.User == nil || (c.AuthorID != v.User.ID && !v.isStaffFor(c)) {
		return nil, apperr.ErrForbidden
	}
	events, err := q.ListEvents(ctx, contentID)
	if err != nil {
		return nil, err
	}
	out := make([]EventView, 0, len(events))
	staff := v.isStaffFor(c)
	for _, e := range events {
		ev := EventView{StatusEvent: e}
		if staff {
			ev.ActorID = e.ActorID
		}
		out = append(out, ev)
	}
	return out, nil
}

// ReviewView hides reviewer identity from authors.
type ReviewView struct {
	db.Review
	ReviewerID *int64 `json:"reviewer_id"`
}

func (s *Service) Reviews(ctx context.Context, v Viewer, contentID int64) ([]ReviewView, error) {
	q := db.New(s.pool)
	c, err := q.GetContent(ctx, contentID)
	if err != nil {
		return nil, apperr.ErrNotFound
	}
	if v.User == nil || (c.AuthorID != v.User.ID && !v.isStaffFor(c)) {
		return nil, apperr.ErrForbidden
	}
	rows, err := q.ListReviewsForContent(ctx, contentID)
	if err != nil {
		return nil, err
	}
	staff := v.isStaffFor(c)
	out := make([]ReviewView, 0, len(rows))
	for _, r := range rows {
		rv := ReviewView{Review: r}
		if staff {
			id := r.ReviewerID
			rv.ReviewerID = &id
		}
		out = append(out, rv)
	}
	return out, nil
}

// Reports are staff-only, scoped to the moderator's categories; reporter
// identities are never exposed to authors.
func (s *Service) ReportsForContent(ctx context.Context, v Viewer, contentID int64) ([]db.Report, error) {
	q := db.New(s.pool)
	c, err := q.GetContent(ctx, contentID)
	if err != nil {
		return nil, apperr.ErrNotFound
	}
	if !v.isStaffFor(c) {
		return nil, apperr.ErrForbidden
	}
	return q.ListReportsForContent(ctx, contentID)
}
