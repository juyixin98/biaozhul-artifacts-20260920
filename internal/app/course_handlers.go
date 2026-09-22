package app

import (
	"context"
	"errors"
	"fmt"
	"net/http"

	"github.com/jackc/pgx/v5/pgtype"

	"community-governance/internal/database"
	"community-governance/internal/httpx"
)

type lessonInput struct {
	Position         int32  `json:"position"`
	Title            string `json:"title"`
	ContentVersionID int64  `json:"content_version_id"`
}

type moduleInput struct {
	Position int32         `json:"position"`
	Title    string        `json:"title"`
	Lessons  []lessonInput `json:"lessons"`
}

func (a *App) createCourse(w http.ResponseWriter, r *http.Request) {
	cid, ok := a.loadCommunityOr404(w, r)
	if !ok {
		return
	}
	u := currentUser(r)
	var req struct {
		Title         string        `json:"title"`
		RequiredLevel int32         `json:"required_level"`
		Modules       []moduleInput `json:"modules"`
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
	if err := validateModules(req.Modules); err != nil {
		badRequest(w, err.Error())
		return
	}

	var course database.Course
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		var err error
		course, err = q.CreateCourse(r.Context(), database.CreateCourseParams{
			CommunityID: cid, AuthorID: u.ID, Title: req.Title,
			RequiredLevel: req.RequiredLevel,
		})
		if err != nil {
			return err
		}
		return writeDraftStructure(r.Context(), q, course.ID, req.Modules)
	})
	if txErr != nil {
		writeErr(w, txErr)
		return
	}
	httpx.JSON(w, http.StatusCreated, course)
}

func validateModules(ms []moduleInput) error {
	if len(ms) == 0 {
		return errors.New("at least one module is required")
	}
	seenMod := map[int32]bool{}
	for _, m := range ms {
		if m.Title == "" {
			return errors.New("module title required")
		}
		if seenMod[m.Position] {
			return fmt.Errorf("duplicate module position %d", m.Position)
		}
		seenMod[m.Position] = true
		seenLes := map[int32]bool{}
		for _, l := range m.Lessons {
			if l.Title == "" || l.ContentVersionID <= 0 {
				return errors.New("lesson title and content_version_id required")
			}
			if seenLes[l.Position] {
				return fmt.Errorf("duplicate lesson position %d in module %d", l.Position, m.Position)
			}
			seenLes[l.Position] = true
		}
	}
	return nil
}

func writeDraftStructure(ctx context.Context, q *database.Queries, courseID int64, ms []moduleInput) error {
	if err := q.DeleteModulesForCourse(ctx, courseID); err != nil {
		return err
	}
	for _, m := range ms {
		mod, err := q.CreateModule(ctx, database.CreateModuleParams{
			CourseID: courseID, Position: m.Position, Title: m.Title,
		})
		if err != nil {
			return err
		}
		for _, l := range m.Lessons {
			if _, err := q.CreateLesson(ctx, database.CreateLessonParams{
				ModuleID: mod.ID, Position: l.Position, Title: l.Title,
				ContentVersionID: pgtype.Int8{Int64: l.ContentVersionID, Valid: true},
			}); err != nil {
				return err
			}
		}
	}
	return nil
}

func (a *App) listCourses(w http.ResponseWriter, r *http.Request) {
	cid, ok := a.loadCommunityOr404(w, r)
	if !ok {
		return
	}
	u := currentUser(r)
	var pub pgtype.Bool
	switch r.URL.Query().Get("published") {
	case "true":
		pub = pgtype.Bool{Bool: true, Valid: true}
	case "false":
		if !u.isModerator() {
			forbidden(w, "only moderators may list draft courses")
			return
		}
		pub = pgtype.Bool{Bool: false, Valid: true}
	}
	rows, err := a.Q.ListCourses(r.Context(),
		database.ListCoursesParams{CommunityID: cid, Published: pub})
	if err != nil {
		serverError(w, err)
		return
	}
	out := make([]database.Course, 0, len(rows))
	for _, c := range rows {
		if c.Published || u.isModerator() || c.AuthorID == u.ID {
			// Members: hide published courses above their level from listing.
			if c.Published && !u.isModerator() && c.AuthorID != u.ID {
				level, err := a.Q.GetEffectiveLevel(r.Context(), database.GetEffectiveLevelParams{
					CommunityID: cid, UserID: u.ID,
				})
				if err != nil || level < c.RequiredLevel {
					continue
				}
			}
			out = append(out, c)
		}
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"courses": out})
}

// getCourse returns the published snapshot to readers, and the editable draft
// structure to the author / moderators.
func (a *App) getCourse(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	c, err := a.Q.GetCourse(r.Context(),
		database.GetCourseParams{ID: id, CommunityID: u.CommunityID})
	if err != nil {
		writeErr(w, err)
		return
	}
	resp := map[string]any{"course": c}

	if c.Published && (u.isModerator() || u.ID != c.AuthorID) {
		// Readers always consume the frozen snapshot.
		pub, err := a.Q.GetLatestPublish(r.Context(),
			database.GetLatestPublishParams{CourseID: id, CommunityID: u.CommunityID})
		if err != nil {
			writeErr(w, err)
			return
		}
		snap, err := a.loadSnapshot(r.Context(), pub.ID)
		if err != nil {
			serverError(w, err)
			return
		}
		resp["publish"] = pub
		resp["structure"] = snap
		httpx.JSON(w, http.StatusOK, resp)
		return
	}

	// Author/mod may inspect the draft structure too.
	if u.isModerator() || c.AuthorID == u.ID {
		mods, err := a.Q.ListDraftModules(r.Context(), id)
		if err != nil {
			serverError(w, err)
			return
		}
		lessons, err := a.Q.ListDraftLessons(r.Context(), id)
		if err != nil {
			serverError(w, err)
			return
		}
		resp["draft_modules"] = mods
		resp["draft_lessons"] = lessons
	}
	httpx.JSON(w, http.StatusOK, resp)
}

// replaceCourseStructure edits the draft only. Already-published snapshots are
// never touched.
func (a *App) replaceCourseStructure(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	var req struct {
		Modules []moduleInput `json:"modules"`
	}
	if err := httpx.Decode(r, &req); err != nil || req.Modules == nil {
		badRequest(w, "modules required")
		return
	}
	if err := validateModules(req.Modules); err != nil {
		badRequest(w, err.Error())
		return
	}
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		c, err := q.GetCourseForUpdate(r.Context(),
			database.GetCourseForUpdateParams{ID: id, CommunityID: u.CommunityID})
		if err != nil {
			return err
		}
		if c.AuthorID != u.ID && !u.isModerator() {
			return errForbidden
		}
		return writeDraftStructure(r.Context(), q, id, req.Modules)
	})
	if txErr != nil {
		writeErr(w, txErr)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

type snapshotModule struct {
	Position int32                          `json:"position"`
	Title    string                         `json:"title"`
	Lessons  []database.CoursePublishLesson `json:"lessons"`
}

func (a *App) loadSnapshot(ctx context.Context, publishID int64) ([]snapshotModule, error) {
	mods, err := a.Q.ListPublishModules(ctx, publishID)
	if err != nil {
		return nil, err
	}
	out := make([]snapshotModule, 0, len(mods))
	for _, m := range mods {
		ls, err := a.Q.ListPublishLessons(ctx, m.ID)
		if err != nil {
			return nil, err
		}
		out = append(out, snapshotModule{Position: m.Position, Title: m.Title, Lessons: ls})
	}
	return out, nil
}

// publishCourse freezes the current draft: every lesson must reference a
// published, non-delisted content version and each referenced content's tier
// must be <= the course tier. A new snapshot replaces the previous one; old
// snapshots remain for history.
func (a *App) publishCourse(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)

	var pub database.CoursePublish
	var snap []snapshotModule
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		c, err := q.GetCourseForUpdate(r.Context(),
			database.GetCourseForUpdateParams{ID: id, CommunityID: u.CommunityID})
		if err != nil {
			return err
		}
		if c.AuthorID != u.ID && !u.isModerator() {
			return errForbidden
		}

		// Re-validate ALL referenced versions at publish time.
		rows, err := q.ValidateCourseDraft(r.Context(), id)
		if err != nil {
			return err
		}
		if len(rows) == 0 {
			return fmt.Errorf("course has no lessons: %w", errBadRequest)
		}
		for _, row := range rows {
			if !row.VersionID.Valid {
				return fmt.Errorf("lesson %d/%d has no content version: %w",
					row.ModulePos, row.LessonPos, errBadRequest)
			}
			if row.ContentStatus.String != "published" {
				return fmt.Errorf("lesson %d/%d content is not published (status=%s): %w",
					row.ModulePos, row.LessonPos, row.ContentStatus.String, errConflict)
			}
			if !row.PublishedVersionID.Valid ||
				row.PublishedVersionID.Int64 != row.VersionID.Int64 {
				return fmt.Errorf("lesson %d/%d references a non-published version: %w",
					row.ModulePos, row.LessonPos, errConflict)
			}
			if row.ContentLevel.Int32 > c.RequiredLevel {
				return fmt.Errorf("lesson %d/%d requires level %d but course is level %d: %w",
					row.ModulePos, row.LessonPos,
					row.ContentLevel.Int32, c.RequiredLevel, errForbidden)
			}
		}

		pub, err = q.CreatePublish(r.Context(), database.CreatePublishParams{
			CourseID: id, CommunityID: u.CommunityID, PublishedBy: u.ID,
		})
		if err != nil {
			return err
		}

		// Freeze structure and content versions into snapshot tables.
		mods, err := q.ListDraftModules(r.Context(), id)
		if err != nil {
			return err
		}
		for _, m := range mods {
			pm, err := q.CreatePublishModule(r.Context(), database.CreatePublishModuleParams{
				PublishID: pub.ID, Position: m.Position, Title: m.Title,
			})
			if err != nil {
				return err
			}
			for _, row := range rows {
				if row.ModulePos != m.Position {
					continue
				}
				if _, err := q.CreatePublishLesson(r.Context(), database.CreatePublishLessonParams{
					PublishModuleID:  pm.ID,
					Position:         row.LessonPos,
					Title:            row.Title,
					ContentVersionID: row.VersionID.Int64,
					ContentID:        row.ContentID.Int64,
					RequiredLevel:    row.ContentLevel.Int32,
				}); err != nil {
					return err
				}
			}
		}

		if err := q.MarkCoursePublished(r.Context(),
			database.MarkCoursePublishedParams{ID: id, CommunityID: u.CommunityID}); err != nil {
			return err
		}
		snap, err = a.loadSnapshot(r.Context(), pub.ID)
		return err
	})
	if txErr != nil {
		writeErr(w, txErr)
		return
	}
	httpx.JSON(w, http.StatusCreated, map[string]any{
		"publish": pub, "structure": snap,
	})
}

// getLesson reads one frozen lesson of the LATEST published snapshot, then
// applies the exact same content authorization as direct content access.
func (a *App) getLesson(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	pos, err := urlID(r, "pos")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	c, err := a.Q.GetCourse(r.Context(),
		database.GetCourseParams{ID: id, CommunityID: u.CommunityID})
	if err != nil {
		writeErr(w, err)
		return
	}
	if !c.Published {
		notFound(w, "course is not published")
		return
	}
	// Course-level tier gate (members need at least the course's own level).
	if !u.isModerator() && c.AuthorID != u.ID {
		level, err := a.Q.GetEffectiveLevel(r.Context(), database.GetEffectiveLevelParams{
			CommunityID: u.CommunityID, UserID: u.ID,
		})
		if err != nil {
			forbidden(w, "active membership required")
			return
		}
		if level < c.RequiredLevel {
			forbidden(w, "your membership tier does not include this course")
			return
		}
	}

	pub, err := a.Q.GetLatestPublish(r.Context(),
		database.GetLatestPublishParams{CourseID: id, CommunityID: u.CommunityID})
	if err != nil {
		writeErr(w, err)
		return
	}
	mods, err := a.Q.ListPublishModules(r.Context(), pub.ID)
	if err != nil {
		serverError(w, err)
		return
	}
	type foundLesson struct {
		ModuleTitle string
		Lesson      database.CoursePublishLesson
	}
	var found *foundLesson
	for _, m := range mods {
		ls, err := a.Q.ListPublishLessons(r.Context(), m.ID)
		if err != nil {
			serverError(w, err)
			return
		}
		for _, l := range ls {
			if l.Position == int32(pos) {
				found = &foundLesson{ModuleTitle: m.Title, Lesson: l}
			}
		}
	}
	if found == nil {
		notFound(w, "lesson not found")
		return
	}

	// Same choke-point as body / attachment / export. The frozen version is
	// authorized exactly like direct content access; if the content has since
	// been delisted, members lose access immediately.
	res, err := a.resolveReadableVersion(r.Context(), a.Q, u,
		found.Lesson.ContentID, found.Lesson.ContentVersionID)
	if err != nil {
		forbidden(w, err.Error())
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{
		"module_title": found.ModuleTitle,
		"lesson":       found.Lesson,
		"content":      res.Content,
		"version":      res.Version,
	})
}
