package handlers

import (
	"net/http"

	"communitygov/internal/services"
)

type createCourseReq struct {
	Title string `json:"title"`
}

type setStructureReq struct {
	Modules []services.ModuleInput `json:"modules"`
}

func (h *Handlers) CreateCourse(w http.ResponseWriter, r *http.Request) {
	var req createCourseReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return
	}
	cid := urlInt64(r, "communityID")
	c, err := h.Courses.Create(r.Context(), cid, req.Title)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, c)
}

func (h *Handlers) SetCourseStructure(w http.ResponseWriter, r *http.Request) {
	var req setStructureReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return
	}
	cid := urlInt64(r, "communityID")
	courseID := urlInt64(r, "courseID")
	if err := h.Courses.SetStructure(r.Context(), courseID, cid, req.Modules); err != nil {
		writeErr(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (h *Handlers) PublishCourse(w http.ResponseWriter, r *http.Request) {
	cid := urlInt64(r, "communityID")
	courseID := urlInt64(r, "courseID")
	c, err := h.Courses.Publish(r.Context(), courseID, cid, currentUser(r).ID)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, c)
}

func (h *Handlers) UnpublishCourse(w http.ResponseWriter, r *http.Request) {
	cid := urlInt64(r, "communityID")
	courseID := urlInt64(r, "courseID")
	c, err := h.Courses.Unpublish(r.Context(), courseID, cid)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, c)
}

// GetPublishedCourse returns the frozen published snapshot. Access reuses the
// unified content gate; each lesson version is independently re-gated when the
// learner opens it, so tier and expiry isolation is identical to post reads.
func (h *Handlers) GetPublishedCourse(w http.ResponseWriter, r *http.Request) {
	cid := urlInt64(r, "communityID")
	courseID := urlInt64(r, "courseID")
	view, err := h.Courses.GetPublished(r.Context(), courseID)
	if err != nil {
		writeErr(w, err)
		return
	}
	rc := h.readContext(r, cid)
	for _, m := range view.Modules {
		for _, l := range m.Lessons {
			if _, err := h.Posts.AuthorizeVersion(r.Context(), l.ContentVersionID, rc); err != nil {
				writeErr(w, err)
				return
			}
		}
	}
	writeJSON(w, http.StatusOK, view)
}

func (h *Handlers) GetDraftCourse(w http.ResponseWriter, r *http.Request) {
	courseID := urlInt64(r, "courseID")
	view, err := h.Courses.GetDraft(r.Context(), courseID)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (h *Handlers) ListCourses(w http.ResponseWriter, r *http.Request) {
	cs, err := h.Courses.List(r.Context(), urlInt64(r, "communityID"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, cs)
}
