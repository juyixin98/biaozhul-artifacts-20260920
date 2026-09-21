package service

import (
	"context"
	"errors"
	"strings"

	"github.com/jackc/pgx/v5"

	"communityvault/internal/auth"
	db "communityvault/internal/db"
)

// ReportResult reports whether a NEW report row was created.
type ReportResult struct {
	Created bool   `json:"created"` // false for an idempotent duplicate
	ID      int64  `json:"id,omitempty"`
	Message string `json:"message"`
}

// Report creates a report keyed by (reporter, content). The unique constraint
// plus ON CONFLICT DO NOTHING make repeated reports by the same user idempotent:
// they never add a row, never bump a count.
func (s *Service) Report(ctx context.Context, actor *auth.Principal, contentID int64, reason string) (ReportResult, error) {
	if strings.TrimSpace(reason) == "" {
		return ReportResult{}, ErrValidation
	}
	if _, err := s.q.GetContent(ctx, contentID); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return ReportResult{}, ErrNotFound
		}
		return ReportResult{}, err
	}
	row, err := s.q.InsertReport(ctx, db.InsertReportParams{
		ContentID: contentID, ReporterID: actor.ID, Reason: reason,
	})
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return ReportResult{}, err
	}
	// ON CONFLICT DO NOTHING with RETURNING yields no row on a duplicate.
	if errors.Is(err, pgx.ErrNoRows) {
		existing, gerr := s.q.GetReport(ctx, db.GetReportParams{
			ContentID: contentID, ReporterID: actor.ID,
		})
		if gerr != nil {
			return ReportResult{}, gerr
		}
		return ReportResult{
			Created: false, ID: existing.ID,
			Message: "you have already reported this content; duplicate reports are not counted",
		}, nil
	}
	return ReportResult{
		Created: true, ID: row.ID,
		Message: "report recorded",
	}, nil
}

// ReportSummary hides reporter ids/reasons from ordinary users. Only an
// in-scope moderator or admin sees reporter-identifying evidence.
type ReportSummary struct {
	ID         int64  `json:"id"`
	Status     string `json:"status"`
	Reason     string `json:"reason,omitempty"`
	ReporterID int64  `json:"reporter_id,omitempty"`
	CreatedAt  string `json:"created_at"`
}

// ReportAggregate is the author-facing, privacy-reduced view: counts only,
// never reporter identities or raw reasons.
type ReportAggregate struct {
	Total int `json:"total"`
	Open  int `json:"open"`
}

// ListReports returns reports for a content. Moderators/admins with scope see
// full evidence; the author sees only aggregate counts, never who reported or
// the raw reason.
func (s *Service) ListReports(ctx context.Context, actor *auth.Principal, contentID int64) (any, error) {
	c, err := s.q.GetContent(ctx, contentID)
	if err == pgx.ErrNoRows {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	isMod := actor.CanModerate(c.Category)
	rows, err := s.q.ListReportsForContent(ctx, contentID)
	if err != nil {
		return nil, err
	}

	if !isMod {
		if c.AuthorID != actor.ID {
			return nil, ErrForbidden
		}
		v := ReportAggregate{}
		for _, r := range rows {
			v.Total++
			if r.Status == "open" {
				v.Open++
			}
		}
		return v, nil
	}

	out := make([]ReportSummary, 0, len(rows))
	for _, r := range rows {
		out = append(out, ReportSummary{
			ID: r.ID, Status: r.Status, Reason: r.Reason,
			ReporterID: r.ReporterID,
			CreatedAt:  r.CreatedAt.Time.UTC().Format("2006-01-02T15:04:05.000Z"),
		})
	}
	return out, nil
}

// ResolveReport closes a report. In-scope moderator or admin only.
func (s *Service) ResolveReport(ctx context.Context, actor *auth.Principal, reportID int64) error {
	if !actor.IsAdmin() && !actor.IsModerator() {
		return ErrForbidden
	}
	return s.withTx(ctx, func(q *db.Queries, _ pgx.Tx) error {
		contentID, err := q.QueryReportContent(ctx, reportID)
		if err == pgx.ErrNoRows {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		c, err := q.GetContent(ctx, contentID)
		if err != nil {
			return err
		}
		if !actor.CanModerate(c.Category) {
			return ErrForbidden
		}
		return q.ResolveReport(ctx, reportID)
	})
}
