package api

import (
	"fmt"
	"net/http"

	"proofcycle/internal/middleware"

	"github.com/gin-gonic/gin"
)

// readUpload extracts the multipart file. The body limit is enforced by the
// router's MaxMultipartMemory plus the storage layer's streaming size check.
func (h *Handler) readUpload(c *gin.Context) (multipartFile, string, bool) {
	fh, err := c.FormFile("file")
	if err != nil {
		fail(c, http.StatusBadRequest, `multipart form field "file" is required`)
		return nil, "", false
	}
	f, err := fh.Open()
	if err != nil {
		fail(c, http.StatusBadRequest, "could not read uploaded file")
		return nil, "", false
	}
	return f, fh.Filename, true
}

type multipartFile interface {
	Read(p []byte) (int, error)
	Close() error
}

func (h *Handler) uploadVersion(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	f, name, ok := h.readUpload(c)
	if !ok {
		return
	}
	defer f.Close()

	v, round, err := h.svc.SaveFirstVersion(id, middleware.User(c).ID, name, f)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"version": v, "round": round})
}

func (h *Handler) uploadRevision(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	f, name, ok := h.readUpload(c)
	if !ok {
		return
	}
	defer f.Close()

	v, round, err := h.svc.SubmitRevision(id, middleware.User(c).ID, name, f)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{
		"version": v,
		"round":   round,
		"notice":  "previous version opinions are retained as history but cannot approve this new version",
	})
}

func (h *Handler) listVersions(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	if !h.requireMember(c, id) {
		return
	}
	versions, err := h.svc.ListVersions(id)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"versions": versions})
}

func (h *Handler) getVersion(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	ver, ok := parseID(c, "ver")
	if !ok {
		return
	}
	if !h.requireMember(c, id) {
		return
	}
	v, err := h.svc.GetVersion(id, int(ver))
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"version": v})
}

func (h *Handler) downloadVersion(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	ver, ok := parseID(c, "ver")
	if !ok {
		return
	}
	rc, v, err := h.svc.OpenVersion(id, middleware.User(c).ID, int(ver))
	if err != nil {
		writeServiceError(c, err)
		return
	}
	defer rc.Close()
	contentType := "application/pdf"
	if v.Kind == "png" {
		contentType = "image/png"
	}
	c.Header("X-Content-SHA256", v.SHA256)
	c.Header("Content-Disposition", fmt.Sprintf(`attachment; filename="v%d-%s"`, v.Version, safeHeaderName(v.Filename)))
	c.DataFromReader(http.StatusOK, v.Size, contentType, rc, nil)
}

// safeHeaderName strips characters that are unsafe in a Content-Disposition
// filename parameter.
func safeHeaderName(name string) string {
	var b []byte
	for _, r := range name {
		switch r {
		case '"', '\\', '\r', '\n':
			continue
		}
		if r > 126 {
			continue
		}
		b = append(b, byte(r))
	}
	if len(b) == 0 {
		return "file"
	}
	return string(b)
}

func (h *Handler) activeRound(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	if !h.requireMember(c, id) {
		return
	}
	round, err := h.svc.ActiveRound(id)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	detail, err := h.svc.History(id, middleware.User(c).ID, round.VersionNumber)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"round": detail})
}

// requireMember authorizes the actor against a job and returns false if the
// request was already rejected (404 for non-members, so jobs do not leak).
func (h *Handler) requireMember(c *gin.Context, id uint) bool {
	job, reviewers, found, err := h.svc.GetJob(id)
	if err != nil {
		writeServiceError(c, err)
		return false
	}
	if !found {
		fail(c, http.StatusNotFound, "not found")
		return false
	}
	u := middleware.User(c)
	if u.ID != job.DesignerID && u.ID != job.PMID && !isRoster(u.ID, reviewers) {
		fail(c, http.StatusNotFound, "not found")
		return false
	}
	return true
}
