package services

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"communitygov/internal/database"
	"communitygov/internal/database/sqlcgen"
)

type ReportService struct {
	store *database.Store
	posts *PostService
}

func NewReportService(s *database.Store, posts *PostService) *ReportService {
	return &ReportService{store: s, posts: posts}
}

// File opens a report against a specific post version.
func (s *ReportService) File(ctx context.Context, communityID, reporterID, postID, versionID int64,
	category, reason string) (sqlcgen.Report, error) {
	if category == "" || reason == "" {
		return sqlcgen.Report{}, E(ErrValidation, "category and reason are required")
	}
	var rep sqlcgen.Report
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		p, err := q.GetPost(ctx, postID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "post")
		} else if err != nil {
			return err
		}
		if p.CommunityID != communityID {
			return E(ErrNotFound, "post not in community")
		}
		v, err := q.GetVersion(ctx, versionID)
		if errors.Is(err, pgx.ErrNoRows) || v.PostID != postID {
			return E(ErrNotFound, "target version")
		} else if err != nil {
			return err
		}
		rep, err = q.CreateReport(ctx, sqlcgen.CreateReportParams{
			CommunityID: communityID, ReporterID: reporterID, PostID: postID,
			TargetVersionID: versionID, Category: category, Reason: reason,
		})
		return err
	})
	return rep, err
}

// Accept triages a filed report: filed -> accepted.
func (s *ReportService) Accept(ctx context.Context, reportID, communityID, actorID int64, reason string) (sqlcgen.Report, error) {
	return s.transition(ctx, reportID, communityID, actorID, "accept", reason,
		map[string]string{"filed": "accepted"})
}

// Reject closes a filed/accepted report without action.
func (s *ReportService) Reject(ctx context.Context, reportID, communityID, actorID int64, reason string) (sqlcgen.Report, error) {
	return s.transition(ctx, reportID, communityID, actorID, "reject", reason,
		map[string]string{"filed": "rejected", "accepted": "rejected"})
}

// Uphold rules the content violating: accepted/appealed -> upheld, and takes
// the reported post down immediately (actor + basis recorded in both places).
func (s *ReportService) Uphold(ctx context.Context, reportID, communityID, actorID int64,
	reason string) (sqlcgen.Report, error) {
	var rep sqlcgen.Report
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		r, err := q.GetReportForUpdate(ctx, reportID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "report")
		} else if err != nil {
			return err
		}
		if r.CommunityID != communityID {
			return E(ErrNotFound, "report not in community")
		}
		if r.Status != "accepted" && r.Status != "appealed" {
			return E(ErrReportState, "only accepted or appealed reports can be upheld")
		}
		// Take down the live post. target version's post is locked inside.
		if _, err := s.posts.Takedown(ctx, r.PostID, communityID, actorID,
			"report #"+itoa(r.ID)+" upheld: "+reason, true); err != nil {
			return err
		}
		rep, err = q.SetReportStatus(ctx,
			sqlcgen.SetReportStatusParams{ID: reportID, Status: "upheld"})
		if err != nil {
			return err
		}
		return logDecision(ctx, q, reportID, "uphold", actorID, reason)
	})
	return rep, err
}

// Overturn clears the accused content after accept: accepted/appealed -> overturned.
func (s *ReportService) Overturn(ctx context.Context, reportID, communityID, actorID int64,
	reason string) (sqlcgen.Report, error) {
	return s.transition(ctx, reportID, communityID, actorID, "overturn", reason,
		map[string]string{"accepted": "overturned", "appealed": "overturned"})
}

// Appeal is allowed exactly once, from a terminal upheld/overturned/rejected
// state, and moves the report to 'appealed' for a fresh moderator decision.
func (s *ReportService) Appeal(ctx context.Context, reportID, communityID, actorID int64,
	reason string) (sqlcgen.Report, error) {
	var rep sqlcgen.Report
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		r, err := q.GetReportForUpdate(ctx, reportID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "report")
		} else if err != nil {
			return err
		}
		if r.CommunityID != communityID {
			return E(ErrNotFound, "report not in community")
		}
		// Only the author of the reported post, or the original reporter, may
		// appeal.
		p, err := q.GetPost(ctx, r.PostID)
		if err != nil {
			return err
		}
		if actorID != p.AuthorID && actorID != r.ReporterID {
			return E(ErrForbidden, "only the report parties may appeal")
		}
		switch r.Status {
		case "upheld", "overturned", "rejected":
		default:
			return E(ErrReportState, "report is not in an appealable state")
		}
		cnt, err := q.CountAppeals(ctx, reportID)
		if err != nil {
			return err
		}
		if cnt > 0 {
			return E(ErrAppealUsed, "")
		}
		rep, err = q.SetReportStatus(ctx,
			sqlcgen.SetReportStatusParams{ID: reportID, Status: "appealed"})
		if err != nil {
			return err
		}
		return logDecision(ctx, q, reportID, "appeal", actorID, reason)
	})
	return rep, err
}

// Restore re-publishes the post's approved version after an upheld report is
// reversed. Refuses when a newer version exists, so a restore never clobbers
// later edits; actor and basis are recorded.
func (s *ReportService) Restore(ctx context.Context, reportID, communityID, actorID int64,
	versionID int64, reason string) (sqlcgen.Report, sqlcgen.Post, error) {
	var rep sqlcgen.Report
	var post sqlcgen.Post
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		r, err := q.GetReportForUpdate(ctx, reportID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "report")
		} else if err != nil {
			return err
		}
		if r.CommunityID != communityID {
			return E(ErrNotFound, "report not in community")
		}
		if r.Status != "upheld" && r.Status != "overturned" {
			return E(ErrReportState, "only an upheld or overturned report can be restored from")
		}
		post, err = s.posts.Restore(ctx, r.PostID, communityID, actorID, versionID,
			"report #"+itoa(r.ID)+": "+reason)
		if err != nil {
			return err
		}
		rep, err = q.SetReportStatus(ctx,
			sqlcgen.SetReportStatusParams{ID: reportID, Status: "restored"})
		if err != nil {
			return err
		}
		return logDecision(ctx, q, reportID, "restore", actorID, reason)
	})
	return rep, post, err
}

func (s *ReportService) Get(ctx context.Context, id int64) (sqlcgen.Report, error) {
	r, err := s.store.GetReport(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return sqlcgen.Report{}, E(ErrNotFound, "report")
	}
	return r, err
}

func (s *ReportService) List(ctx context.Context, communityID int64, status string,
	limit, offset int32) ([]sqlcgen.Report, error) {
	p := sqlcgen.ListReportsParams{CommunityID: communityID, Limit: limit, Offset: offset}
	if status != "" {
		p.Status = pgtype.Text{String: status, Valid: true}
	}
	return s.store.ListReports(ctx, p)
}

func (s *ReportService) Decisions(ctx context.Context, reportID int64) ([]sqlcgen.ReportDecision, error) {
	return s.store.ListDecisions(ctx, reportID)
}

func (s *ReportService) transition(ctx context.Context, reportID, communityID, actorID int64,
	action, reason string, allowed map[string]string) (sqlcgen.Report, error) {
	var rep sqlcgen.Report
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		r, err := q.GetReportForUpdate(ctx, reportID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "report")
		} else if err != nil {
			return err
		}
		if r.CommunityID != communityID {
			return E(ErrNotFound, "report not in community")
		}
		next, ok := allowed[r.Status]
		if !ok {
			return E(ErrReportState, "action "+action+" illegal from status "+r.Status)
		}
		rep, err = q.SetReportStatus(ctx,
			sqlcgen.SetReportStatusParams{ID: reportID, Status: next})
		if err != nil {
			return err
		}
		return logDecision(ctx, q, reportID, action, actorID, reason)
	})
	return rep, err
}

func logDecision(ctx context.Context, q *sqlcgen.Queries, reportID int64,
	action string, actorID int64, reason string) error {
	_, err := q.InsertDecision(ctx, sqlcgen.InsertDecisionParams{
		ReportID: reportID, Action: action, ActorID: actorID, Reason: reason,
	})
	return err
}

func itoa(v int64) string {
	if v == 0 {
		return "0"
	}
	neg := v < 0
	if neg {
		v = -v
	}
	var b [20]byte
	i := len(b)
	for v > 0 {
		i--
		b[i] = byte('0' + v%10)
		v /= 10
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:])
}
