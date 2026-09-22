package handlers

import (
	"io"
	"net/http"

	"communitygov/internal/services"
)

type createPostReq struct {
	RequiredTierLevel int32  `json:"required_tier_level"`
	Title             string `json:"title"`
	Body              string `json:"body"`
}

func (h *Handlers) CreatePost(w http.ResponseWriter, r *http.Request) {
	var req createPostReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return
	}
	cid := urlInt64(r, "communityID")
	u := currentUser(r)
	if req.RequiredTierLevel == 0 {
		req.RequiredTierLevel = 1
	}
	p, v, err := h.Posts.CreatePost(r.Context(), cid, u.ID,
		req.RequiredTierLevel, req.Title, req.Body)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"post": p, "version": v})
}

type editPostReq struct {
	Title             string `json:"title"`
	Body              string `json:"body"`
	RequiredTierLevel int32  `json:"required_tier_level"`
}

func (h *Handlers) EditPost(w http.ResponseWriter, r *http.Request) {
	var req editPostReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return
	}
	pid := urlInt64(r, "postID")
	u := currentUser(r)
	v, err := h.Posts.AddVersion(r.Context(), pid, u.ID, req.Title, req.Body, req.RequiredTierLevel)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"version": v})
}

func (h *Handlers) SubmitPost(w http.ResponseWriter, r *http.Request) {
	pid := urlInt64(r, "postID")
	p, err := h.Posts.Submit(r.Context(), pid, currentUser(r).ID)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, p)
}

func (h *Handlers) WithdrawPost(w http.ResponseWriter, r *http.Request) {
	pid := urlInt64(r, "postID")
	p, err := h.Posts.Withdraw(r.Context(), pid, currentUser(r).ID)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, p)
}

type reviewReq struct {
	VersionID int64  `json:"version_id"`
	Approve   bool   `json:"approve"`
	Reason    string `json:"reason"`
}

func (h *Handlers) ReviewPost(w http.ResponseWriter, r *http.Request) {
	var req reviewReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return
	}
	cid := urlInt64(r, "communityID")
	pid := urlInt64(r, "postID")
	u := currentUser(r)
	// Reviewer must be assigned to this community; admins moderate everywhere.
	if u.Role != "admin" && !h.Users.IsReviewer(r.Context(), cid, u.ID) {
		writeErr(w, services.E(services.ErrForbidden, "not a reviewer of this community"))
		return
	}
	p, err := h.Posts.Review(r.Context(), services.ReviewAction{
		PostID: pid, CommunityID: cid, ReviewerID: u.ID,
		VersionID: req.VersionID, Approve: req.Approve, Reason: req.Reason,
		// ExpectedStatus is deliberately not supplied: the service derives the
		// correct gate state ('pending' for a first review, 'published' for a
		// re-review of an edit) from the live post. The decision is still bound
		// to that state AND to the exact version id.
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, p)
}

type takedownReq struct {
	Reason         string `json:"reason"`
	IncludePending bool   `json:"include_pending"`
}

func (h *Handlers) TakedownPost(w http.ResponseWriter, r *http.Request) {
	var req takedownReq
	_ = decode(r, &req)
	cid := urlInt64(r, "communityID")
	pid := urlInt64(r, "postID")
	u := currentUser(r)
	if u.Role != "admin" && !h.Users.IsReviewer(r.Context(), cid, u.ID) {
		writeErr(w, services.E(services.ErrForbidden, "not a reviewer of this community"))
		return
	}
	p, err := h.Posts.Takedown(r.Context(), pid, cid, u.ID, req.Reason, req.IncludePending)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, p)
}

type restoreReq struct {
	VersionID int64  `json:"version_id"`
	Reason    string `json:"reason"`
}

func (h *Handlers) RestorePost(w http.ResponseWriter, r *http.Request) {
	var req restoreReq
	if err := decode(r, &req); err != nil {
		writeErr(w, errBadRequest(err.Error()))
		return
	}
	cid := urlInt64(r, "communityID")
	pid := urlInt64(r, "postID")
	u := currentUser(r)
	if u.Role != "admin" && !h.Users.IsReviewer(r.Context(), cid, u.ID) {
		writeErr(w, services.E(services.ErrForbidden, "not a reviewer of this community"))
		return
	}
	p, err := h.Posts.Restore(r.Context(), pid, cid, u.ID, req.VersionID, req.Reason)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, p)
}

func (h *Handlers) GetPost(w http.ResponseWriter, r *http.Request) {
	p, err := h.Posts.GetPost(r.Context(), urlInt64(r, "postID"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, p)
}

func (h *Handlers) ListPosts(w http.ResponseWriter, r *http.Request) {
	cid := urlInt64(r, "communityID")
	limit := queryInt32(r, "limit", 50)
	offset := queryInt32(r, "offset", 0)
	status := r.URL.Query().Get("status")
	var authorID int64
	if r.URL.Query().Get("mine") == "true" {
		authorID = currentUser(r).ID
	}
	posts, err := h.Posts.ListPosts(r.Context(), cid, authorID, status, limit, offset)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, posts)
}

func (h *Handlers) ListVersions(w http.ResponseWriter, r *http.Request) {
	pid := urlInt64(r, "postID")
	vs, err := h.Posts.ListVersions(r.Context(), pid)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, vs)
}

// ReadVersion enforces the unified gate before returning body content.
func (h *Handlers) ReadVersion(w http.ResponseWriter, r *http.Request) {
	cid := urlInt64(r, "communityID")
	vid := urlInt64(r, "versionID")
	v, err := h.Posts.AuthorizeVersion(r.Context(), vid, h.readContext(r, cid))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (h *Handlers) ExportVersion(w http.ResponseWriter, r *http.Request) {
	cid := urlInt64(r, "communityID")
	vid := urlInt64(r, "versionID")
	name, data, err := h.Posts.Export(r.Context(), vid, h.readContext(r, cid))
	if err != nil {
		writeErr(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/zip")
	w.Header().Set("Content-Disposition", `attachment; filename="`+name+`"`)
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(data)
}

// UploadAttachment: multipart/form-data with file field "file".
func (h *Handlers) UploadAttachment(w http.ResponseWriter, r *http.Request) {
	vid := urlInt64(r, "versionID")
	if err := r.ParseMultipartForm(16 << 20); err != nil {
		writeErr(w, errBadRequest("invalid multipart form"))
		return
	}
	file, header, err := r.FormFile("file")
	if err != nil {
		writeErr(w, errBadRequest(`form file field "file" is required`))
		return
	}
	defer file.Close()
	data, err := io.ReadAll(file)
	if err != nil {
		writeErr(w, errBadRequest("could not read upload"))
		return
	}
	ct := header.Header.Get("Content-Type")
	att, err := h.Posts.AddAttachment(r.Context(), vid, currentUser(r).ID,
		header.Filename, ct, data)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"id": att.ID, "version_id": att.VersionID,
		"filename": att.Filename, "content_type": att.ContentType,
	})
}

func (h *Handlers) DownloadAttachment(w http.ResponseWriter, r *http.Request) {
	cid := urlInt64(r, "communityID")
	aid := urlInt64(r, "attachmentID")
	att, err := h.Posts.GetAttachmentForDownload(r.Context(), aid, h.readContext(r, cid))
	if err != nil {
		writeErr(w, err)
		return
	}
	w.Header().Set("Content-Type", att.ContentType)
	w.Header().Set("Content-Disposition", `attachment; filename="`+att.Filename+`"`)
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(att.Data)
}
