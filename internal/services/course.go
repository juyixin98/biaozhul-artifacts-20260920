package services

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"

	"communitygov/internal/database"
	"communitygov/internal/database/sqlcgen"
)

type CourseService struct{ store *database.Store }

func NewCourseService(s *database.Store) *CourseService { return &CourseService{store: s} }

func (s *CourseService) Create(ctx context.Context, communityID int64, title string) (sqlcgen.Course, error) {
	if title == "" {
		return sqlcgen.Course{}, E(ErrValidation, "title required")
	}
	return s.store.CreateCourse(ctx, sqlcgen.CreateCourseParams{CommunityID: communityID, Title: title})
}

// SetStructure replaces the whole ordered draft structure in one call. Only
// draft courses accept edits; a published course is immutable until republished,
// and edits before republish never alter the already-served snapshot.
type LessonInput struct {
	Position         int32  `json:"position"`
	Title            string `json:"title"`
	ContentVersionID int64  `json:"content_version_id"` // 0 = informational lesson
}
type ModuleInput struct {
	Position int32         `json:"position"`
	Title    string        `json:"title"`
	Lessons  []LessonInput `json:"lessons"`
}

func (s *CourseService) SetStructure(ctx context.Context, courseID, communityID int64,
	modules []ModuleInput) error {
	return s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		c, err := q.GetCourse(ctx, courseID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "course")
		} else if err != nil {
			return err
		}
		if c.CommunityID != communityID {
			return E(ErrNotFound, "course not in community")
		}
		// Draft structure is ALWAYS editable — even after publish — because it
		// is independent of the frozen snapshot learners see; changes only take
		// effect on the next (fully revalidated) publish.
		if err := q.DeleteAllModules(ctx, courseID); err != nil {
			return err
		}
		for _, mi := range modules {
			m, err := q.UpsertModule(ctx,
				sqlcgen.UpsertModuleParams{CourseID: courseID, Position: mi.Position, Title: mi.Title})
			if err != nil {
				return err
			}
			for _, li := range mi.Lessons {
				params := sqlcgen.UpsertLessonParams{
					ModuleID: m.ID, Position: li.Position, Title: li.Title,
				}
				if li.ContentVersionID != 0 {
					params.ContentVersionID = toPgInt8(li.ContentVersionID)
				}
				if _, err := q.UpsertLesson(ctx, params); err != nil {
					return err
				}
			}
		}
		return nil
	})
}

// validateStructure checks every referenced content version: it must exist, be
// approved, and still be the live published version of its post. Same checks
// run on every (re)publish, so editing underlying content after a course is
// published forces revalidation before the snapshot can advance.
func (s *CourseService) validateStructure(ctx context.Context, q *sqlcgen.Queries, courseID int64) error {
	rows, err := q.GetDraftStructure(ctx, courseID)
	if err != nil {
		return err
	}
	var ids []int64
	seen := map[int64]bool{}
	for _, r := range rows {
		if !r.ContentVersionID.Valid {
			continue // informational lesson, no gated content
		}
		id := r.ContentVersionID.Int64
		if !seen[id] {
			seen[id] = true
			ids = append(ids, id)
		}
	}
	if len(ids) == 0 {
		return nil
	}
	versions, err := q.GetVersionsForPublish(ctx, ids)
	if err != nil {
		return err
	}
	if len(versions) != len(ids) {
		return E(ErrPublishContent, "a referenced version no longer exists")
	}
	for _, v := range versions {
		if v.ReviewStatus != "approved" {
			return E(ErrPublishContent, "lesson content version is not approved")
		}
		if v.PostStatus != "published" || !v.PublishedVersionID.Valid ||
			v.PublishedVersionID.Int64 != v.ID {
			return E(ErrPublishContent, "lesson content is not the currently published version")
		}
	}
	return nil
}

// Publish validates ALL content permissions/state and freezes structure +
// content versions into an immutable snapshot. Republishing re-runs the full
// validation and produces a new snapshot; meanwhile readers continue to be
// served the previous snapshot until the new one is committed atomically.
func (s *CourseService) Publish(ctx context.Context, courseID, communityID, actorID int64) (sqlcgen.Course, error) {
	var course sqlcgen.Course
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		c, err := q.GetCourse(ctx, courseID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "course")
		} else if err != nil {
			return err
		}
		if c.CommunityID != communityID {
			return E(ErrNotFound, "course not in community")
		}
		if err := s.validateStructure(ctx, q, courseID); err != nil {
			return err
		}
		snap, err := q.InsertSnapshot(ctx,
			sqlcgen.InsertSnapshotParams{CourseID: courseID, Title: c.Title, PublishedBy: actorID})
		if err != nil {
			return err
		}
		modules, err := q.ListDraftModules(ctx, courseID)
		if err != nil {
			return err
		}
		for _, dm := range modules {
			sm, err := q.InsertSnapshotModule(ctx,
				sqlcgen.InsertSnapshotModuleParams{SnapshotID: snap.ID, Position: dm.Position, Title: dm.Title})
			if err != nil {
				return err
			}
			lessons, err := q.ListDraftLessons(ctx, dm.ID)
			if err != nil {
				return err
			}
			for _, dl := range lessons {
				if !dl.ContentVersionID.Valid {
					return E(ErrPublishContent, "every published lesson must reference content")
				}
				if _, err := q.InsertSnapshotLesson(ctx, sqlcgen.InsertSnapshotLessonParams{
					SnapModuleID: sm.ID, Position: dl.Position, Title: dl.Title,
					ContentVersionID: dl.ContentVersionID.Int64,
				}); err != nil {
					return err
				}
			}
		}
		course, err = q.MarkCoursePublished(ctx, sqlcgen.MarkCoursePublishedParams{
			ID: courseID, PublishedSnapshotID: toPgInt8(snap.ID), CommunityID: communityID,
		})
		return err
	})
	return course, err
}

// Unpublish returns the course to draft (structure becomes editable again).
func (s *CourseService) Unpublish(ctx context.Context, courseID, communityID int64) (sqlcgen.Course, error) {
	c, err := s.store.ResetCourseToDraft(ctx,
		sqlcgen.ResetCourseToDraftParams{ID: courseID, CommunityID: communityID})
	if errors.Is(err, pgx.ErrNoRows) {
		return sqlcgen.Course{}, E(ErrNotFound, "course")
	}
	return c, err
}

// PublishedView returns the frozen snapshot a learner sees for a published
// course — structure and content versions from publish time, never the draft.
type LessonView struct {
	Position         int32  `json:"position"`
	Title            string `json:"title"`
	ContentVersionID int64  `json:"content_version_id"`
}
type ModuleView struct {
	Position int32        `json:"position"`
	Title    string       `json:"title"`
	Lessons  []LessonView `json:"lessons"`
}
type PublishedCourseView struct {
	CourseID    int64        `json:"course_id"`
	Title       string       `json:"title"`
	PublishedAt string       `json:"published_at"`
	Modules     []ModuleView `json:"modules"`
}

func (s *CourseService) GetPublished(ctx context.Context, courseID int64) (PublishedCourseView, error) {
	var view PublishedCourseView
	c, err := s.store.GetCourse(ctx, courseID)
	if errors.Is(err, pgx.ErrNoRows) {
		return view, E(ErrNotFound, "course")
	} else if err != nil {
		return view, err
	}
	if c.Status != "published" || !c.PublishedSnapshotID.Valid {
		return view, E(ErrPostState, "course is not published")
	}
	snap, err := s.store.GetSnapshot(ctx, c.PublishedSnapshotID.Int64)
	if err != nil {
		return view, err
	}
	view.CourseID = courseID
	view.Title = snap.Title
	view.PublishedAt = snap.PublishedAt.Format("2006-01-02T15:04:05Z07:00")
	mods, err := s.store.ListSnapshotModules(ctx, snap.ID)
	if err != nil {
		return view, err
	}
	ids := make([]int64, 0, len(mods))
	for _, m := range mods {
		ids = append(ids, m.ID)
	}
	lessons, err := s.store.ListSnapshotLessons(ctx, ids)
	if err != nil {
		return view, err
	}
	byMod := map[int64][]LessonView{}
	for _, l := range lessons {
		byMod[l.SnapModuleID] = append(byMod[l.SnapModuleID], LessonView{
			Position: l.Position, Title: l.Title, ContentVersionID: l.ContentVersionID,
		})
	}
	for _, m := range mods {
		view.Modules = append(view.Modules, ModuleView{
			Position: m.Position, Title: m.Title, Lessons: byMod[m.ID],
		})
	}
	return view, nil
}

// DraftView returns the editable draft structure (authors/managers).
func (s *CourseService) GetDraft(ctx context.Context, courseID int64) (any, error) {
	c, err := s.store.GetCourse(ctx, courseID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, E(ErrNotFound, "course")
	} else if err != nil {
		return nil, err
	}
	mods, err := s.store.ListDraftModules(ctx, courseID)
	if err != nil {
		return nil, err
	}
	type lesson struct {
		Position         int32  `json:"position"`
		Title            string `json:"title"`
		ContentVersionID int64  `json:"content_version_id"`
	}
	type module struct {
		Position int32    `json:"position"`
		Title    string   `json:"title"`
		Lessons  []lesson `json:"lessons"`
	}
	out := struct {
		ID      int64    `json:"id"`
		Title   string   `json:"title"`
		Status  string   `json:"status"`
		Modules []module `json:"modules"`
	}{ID: c.ID, Title: c.Title, Status: c.Status}
	for _, dm := range mods {
		mv := module{Position: dm.Position, Title: dm.Title}
		ls, err := s.store.ListDraftLessons(ctx, dm.ID)
		if err != nil {
			return nil, err
		}
		for _, dl := range ls {
			var cv int64
			if dl.ContentVersionID.Valid {
				cv = dl.ContentVersionID.Int64
			}
			mv.Lessons = append(mv.Lessons, lesson{
				Position: dl.Position, Title: dl.Title, ContentVersionID: cv,
			})
		}
		out.Modules = append(out.Modules, mv)
	}
	return out, nil
}

func (s *CourseService) List(ctx context.Context, communityID int64) ([]sqlcgen.Course, error) {
	return s.store.ListCourses(ctx, communityID)
}
