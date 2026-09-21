package service

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"communityvault/internal/auth"
	db "communityvault/internal/db"
)

// canSeeContent decides who may read non-public content:
//   - published content: any authenticated user
//   - draft/pending/withdrawn: the author, an admin, or a moderator whose
//     category scope covers the content
func canSeeContent(p *auth.Principal, c db.Content) bool {
	if c.Status == "published" {
		return true
	}
	if c.AuthorID == p.ID || p.IsAdmin() {
		return true
	}
	return p.IsModerator() && p.CanModerate(c.Category)
}

// GetContent returns one post. Readers without rights on non-public content
// get ErrNotFound, so existence of hidden content is not revealed.
func (s *Service) GetContent(ctx context.Context, actor *auth.Principal, id int64) (ContentDTO, error) {
	c, err := s.q.GetContent(ctx, id)
	if err == pgx.ErrNoRows {
		return ContentDTO{}, ErrNotFound
	}
	if err != nil {
		return ContentDTO{}, err
	}
	if !canSeeContent(actor, c) {
		return ContentDTO{}, ErrNotFound
	}
	var revPtr *db.ContentRevision
	if c.CurrentRevisionID != nil {
		rev, err := s.q.GetRevision(ctx, *c.CurrentRevisionID)
		if err != nil {
			return ContentDTO{}, err
		}
		revPtr = &rev
	}
	return contentDTO(c, revPtr, true), nil
}

// ListRevisions returns revision history. Bodies are shown only after the same
// visibility check as the content itself.
func (s *Service) ListRevisions(ctx context.Context, actor *auth.Principal, contentID int64) ([]RevisionDTO, error) {
	c, err := s.q.GetContent(ctx, contentID)
	if err == pgx.ErrNoRows {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	if !canSeeContent(actor, c) {
		return nil, ErrNotFound
	}
	rows, err := s.q.ListRevisions(ctx, contentID)
	if err != nil {
		return nil, err
	}
	out := make([]RevisionDTO, 0, len(rows))
	for _, r := range rows {
		out = append(out, revisionDTO(r, true))
	}
	return out, nil
}

const cursorTimeLayout = "2006-01-02T15:04:05.000000Z"

// encodeCursor renders an opaque stable keyset cursor over (updated_at, id).
func encodeCursor(t time.Time, id int64) string {
	raw, _ := json.Marshal(cursorJSON{
		T: t.UTC().Format(cursorTimeLayout), I: id,
	})
	return base64.URLEncoding.EncodeToString(raw)
}

func decodeCursor(s string) (pgtype.Timestamptz, int64, error) {
	raw, err := base64.URLEncoding.DecodeString(s)
	if err != nil {
		return pgtype.Timestamptz{}, 0, err
	}
	var c cursorJSON
	if err := json.Unmarshal(raw, &c); err != nil {
		return pgtype.Timestamptz{}, 0, err
	}
	t, err := time.Parse(cursorTimeLayout, c.T)
	if err != nil {
		return pgtype.Timestamptz{}, 0, err
	}
	var ts pgtype.Timestamptz
	ts.Time = t
	ts.Valid = true
	return ts, c.I, nil
}

type cursorJSON struct {
	T string `json:"t"`
	I int64  `json:"i"`
}

// FeedPage is one stable page; NextCursor is "" when there are no more rows.
type FeedPage struct {
	Items      []ContentDTO `json:"items"`
	NextCursor string       `json:"next_cursor,omitempty"`
}

// Feed returns a stable page of published posts. The query only selects
// status='published', so content created or withdrawn while a reader pages can
// never leak: withdrawn rows stop matching, new drafts were never visible, and
// the (updated_at, id) keyset keeps iteration stable.
func (s *Service) Feed(ctx context.Context, actor *auth.Principal, limit int32, cursor string) (FeedPage, error) {
	if limit <= 0 || limit > 100 {
		limit = 20
	}
	var rows []db.Content
	var err error
	if cursor == "" {
		rows, err = s.q.FeedFirstPage(ctx, limit+1)
	} else {
		afterTS, afterID, derr := decodeCursor(cursor)
		if derr != nil {
			return FeedPage{}, ErrValidation
		}
		rows, err = s.q.FeedNextPage(ctx, db.FeedNextPageParams{
			RowLimit:       limit + 1,
			AfterUpdatedAt: afterTS,
			AfterID:        afterID,
		})
	}
	if err != nil {
		return FeedPage{}, err
	}

	page := FeedPage{Items: []ContentDTO{}}
	hasMore := int32(len(rows)) > limit
	if hasMore {
		rows = rows[:limit]
	}
	for i := range rows {
		// Feed rows are published by construction; bodies are omitted from
		// summaries to keep pages light (fetch one row for the body).
		page.Items = append(page.Items, contentDTO(rows[i], nil, false))
	}
	if hasMore && len(rows) > 0 {
		last := rows[len(rows)-1]
		page.NextCursor = encodeCursor(last.UpdatedAt.Time, last.ID)
	}
	return page, nil
}
