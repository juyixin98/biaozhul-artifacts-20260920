package view

import (
	"context"
	"math"

	"communityvault/internal/db"
)

// FeedItem is one published row: content metadata plus the body of the
// published revision only.
type FeedItem struct {
	ID                int64  `json:"id"`
	CategoryID        int64  `json:"category_id"`
	AuthorID          int64  `json:"author_id"`
	Title             string `json:"title"`
	RevisionNo        int32  `json:"revision_no"`
	Body              string `json:"body"`
	PublishedRevision int64  `json:"published_revision"`
}

// FeedPage is a stable-cursor page.
type FeedPage struct {
	Items      []FeedItem `json:"items"`
	NextCursor int64      `json:"next_cursor"`
	HasMore    bool       `json:"has_more"`
}

// Feed returns published content before cursor (descending id). Keyset
// pagination on the immutable surrogate id means inserts cannot push rows
// between pages and withdrawals simply remove rows from later pages; an
// already-paginated reader never sees unpublished content.
func (s *Service) Feed(ctx context.Context, cursor int64, limit int32) (FeedPage, error) {
	if limit <= 0 || limit > 100 {
		limit = 20
	}
	if cursor == 0 {
		cursor = math.MaxInt64
	}
	q := db.New(s.pool)
	// Fetch limit+1 to determine has_more without an extra query.
	rows, err := q.FeedPublished(ctx, db.FeedPublishedParams{
		ID:    cursor,
		Limit: limit + 1,
	})
	if err != nil {
		return FeedPage{}, err
	}
	page := FeedPage{Items: []FeedItem{}}
	hasMore := int32(len(rows)) > limit
	if hasMore {
		rows = rows[:limit]
	}
	for _, r := range rows {
		pr := int64(0)
		if r.PublishedRevision != nil {
			pr = *r.PublishedRevision
		}
		page.Items = append(page.Items, FeedItem{
			ID: r.ID, CategoryID: r.CategoryID, AuthorID: r.AuthorID,
			Title: r.Title, RevisionNo: r.RevisionNo, Body: r.Body,
			PublishedRevision: pr,
		})
	}
	page.HasMore = hasMore
	if len(rows) > 0 {
		page.NextCursor = rows[len(rows)-1].ID
	}
	return page, nil
}
