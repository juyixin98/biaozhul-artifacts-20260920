package api

import (
	"net/http"
	"strings"

	"proofcycle/internal/middleware"
	"proofcycle/internal/models"
	"proofcycle/internal/service"

	"github.com/gin-gonic/gin"
)

type opinionReq struct {
	Items []struct {
		Code            string `json:"code"`
		Outcome         string `json:"outcome"`
		Reason          string `json:"reason"`
		ExpectedVersion int    `json:"expected_version"`
	} `json:"items"`
}

func (h *Handler) upsertOpinions(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	var req opinionReq
	if err := c.ShouldBindJSON(&req); err != nil {
		fail(c, http.StatusBadRequest, "invalid JSON body")
		return
	}
	u := middleware.User(c)
	if u.Role != models.RoleReviewer {
		fail(c, http.StatusForbidden, "only assigned reviewers may submit opinions")
		return
	}

	batch := make([]service.ItemOpinion, 0, len(req.Items))
	expected := make(map[string]int, len(req.Items))
	seen := map[string]struct{}{}
	for _, it := range req.Items {
		code := strings.TrimSpace(it.Code)
		if code == "" {
			fail(c, http.StatusBadRequest, "every opinion requires a checklist code")
			return
		}
		if _, dup := seen[code]; dup {
			fail(c, http.StatusBadRequest, "duplicate checklist code in one request: "+code)
			return
		}
		seen[code] = struct{}{}
		batch = append(batch, service.ItemOpinion{Code: code, Outcome: it.Outcome, Reason: it.Reason})
		expected[code] = it.ExpectedVersion
	}

	if err := h.svc.UpsertOpinions(id, u.ID, expected, batch); err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "opinions recorded"})
}

func (h *Handler) approve(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	u := middleware.User(c)
	if u.Role == models.RoleDesigner {
		fail(c, http.StatusForbidden, service.ErrSelfApproval.Error())
		return
	}
	if u.Role != models.RolePM {
		fail(c, http.StatusForbidden, "only the assigned project manager may sign off")
		return
	}
	approval, err := h.svc.Approve(id, u.ID)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"approval": approval})
}

func (h *Handler) versionHistory(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	ver, ok := parseID(c, "ver")
	if !ok {
		return
	}
	detail, err := h.svc.History(id, middleware.User(c).ID, int(ver))
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"history": detail})
}

func (h *Handler) fullHistory(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	details, err := h.svc.FullHistory(id, middleware.User(c).ID)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"history": details})
}

func (h *Handler) versionReport(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	ver, ok := parseID(c, "ver")
	if !ok {
		return
	}
	h.renderReport(c, id, int(ver))
}

func (h *Handler) latestReport(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	h.renderReport(c, id, 0) // 0 => latest version
}

func (h *Handler) renderReport(c *gin.Context, id uint, ver int) {
	report, job, detail, err := h.svc.Report(id, middleware.User(c).ID, ver)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	if strings.EqualFold(c.Query("format"), "text") ||
		strings.Contains(c.GetHeader("Accept"), "text/plain") {
		c.Header("Content-Type", "text/plain; charset=utf-8")
		c.String(http.StatusOK, "%s", report)
		return
	}
	c.JSON(http.StatusOK, gin.H{"report_text": report, "job_id": job.ID, "version": detail.Round.VersionNumber})
}
