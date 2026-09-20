// Package handler 暴露 ProofCycle 的 HTTP API。
package handler

import (
	"errors"
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"proofcycle/internal/middleware"
	"proofcycle/internal/service"
	"proofcycle/internal/storage"
)

type Handler struct {
	svc *service.Service
}

// NewRouter 构建路由；所有业务接口都需要 X-User-ID 认证。
func NewRouter(db *gorm.DB, svc *service.Service) *gin.Engine {
	r := gin.New()
	r.Use(gin.Recovery())
	r.GET("/healthz", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"status": "ok"}) })

	h := &Handler{svc: svc}
	api := r.Group("/", middleware.Auth(db))
	api.POST("/jobs", h.createJob)
	api.GET("/jobs/:id", h.getJob)
	api.POST("/jobs/:id/revisions", h.submitRevision)
	api.GET("/jobs/:id/revisions", h.listRevisions)
	api.GET("/jobs/:id/revisions/:vid/history", h.revisionHistory)
	api.GET("/jobs/:id/revisions/:vid/file", h.downloadFile)
	api.PUT("/jobs/:id/revisions/:vid/checklist/:itemId", h.updateChecklistItem)
	api.POST("/jobs/:id/revisions/:vid/comments", h.addComment)
	api.POST("/jobs/:id/revisions/:vid/complete", h.completeReview)
	api.PUT("/comments/:id", h.updateComment)
	api.POST("/jobs/:id/approve", h.approve)
	api.GET("/jobs/:id/report", h.report)
	return r
}

func writeErr(c *gin.Context, err error) {
	status := http.StatusInternalServerError
	switch {
	case errors.Is(err, service.ErrNotFound):
		status = http.StatusNotFound
	case errors.Is(err, service.ErrForbidden):
		status = http.StatusForbidden
	case errors.Is(err, service.ErrConflict), errors.Is(err, service.ErrInvalidState):
		status = http.StatusConflict
	case errors.Is(err, service.ErrBadRequest):
		status = http.StatusBadRequest
	case errors.Is(err, storage.ErrTooLarge):
		status = http.StatusRequestEntityTooLarge
	case errors.Is(err, storage.ErrUnsupportedType):
		status = http.StatusUnsupportedMediaType
	case errors.Is(err, storage.ErrInvalidPath):
		status = http.StatusBadRequest
	}
	c.JSON(status, gin.H{"error": err.Error()})
}

func parseID(c *gin.Context, name string) (uint64, bool) {
	id, err := strconv.ParseUint(c.Param(name), 10, 64)
	if err != nil || id == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid " + name})
		return 0, false
	}
	return id, true
}

func (h *Handler) createJob(c *gin.Context) {
	var in service.CreateJobInput
	if err := c.ShouldBindJSON(&in); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	job, err := h.svc.CreateJob(middleware.CurrentUser(c), in)
	if err != nil {
		writeErr(c, err)
		return
	}
	c.JSON(http.StatusCreated, job)
}

func (h *Handler) getJob(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	job, err := h.svc.GetJob(middleware.CurrentUser(c), id)
	if err != nil {
		writeErr(c, err)
		return
	}
	c.JSON(http.StatusOK, job)
}

// submitRevision 接收 multipart 文件（字段名 file），由服务端生成存储路径。
func (h *Handler) submitRevision(c *gin.Context) {
	jobID, ok := parseID(c, "id")
	if !ok {
		return
	}
	fh, err := c.FormFile("file")
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "multipart file field 'file' is required"})
		return
	}
	f, err := fh.Open()
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	defer f.Close()
	rev, err := h.svc.SubmitRevision(middleware.CurrentUser(c), jobID, fh.Filename, f)
	if err != nil {
		writeErr(c, err)
		return
	}
	c.JSON(http.StatusCreated, rev)
}

func (h *Handler) listRevisions(c *gin.Context) {
	jobID, ok := parseID(c, "id")
	if !ok {
		return
	}
	revs, err := h.svc.ListRevisions(middleware.CurrentUser(c), jobID)
	if err != nil {
		writeErr(c, err)
		return
	}
	c.JSON(http.StatusOK, revs)
}

func (h *Handler) revisionHistory(c *gin.Context) {
	jobID, ok := parseID(c, "id")
	if !ok {
		return
	}
	vid, ok := parseID(c, "vid")
	if !ok {
		return
	}
	hist, err := h.svc.GetRevisionHistory(middleware.CurrentUser(c), jobID, vid)
	if err != nil {
		writeErr(c, err)
		return
	}
	c.JSON(http.StatusOK, hist)
}

func (h *Handler) downloadFile(c *gin.Context) {
	jobID, ok := parseID(c, "id")
	if !ok {
		return
	}
	vid, ok := parseID(c, "vid")
	if !ok {
		return
	}
	abs, rev, err := h.svc.OpenRevisionFile(middleware.CurrentUser(c), jobID, vid)
	if err != nil {
		writeErr(c, err)
		return
	}
	c.Header("Content-Type", rev.Mime)
	c.Header("Content-Disposition", "attachment; filename=\""+rev.FileName+"\"")
	c.File(abs)
}

type checklistUpdateReq struct {
	Status          string `json:"status" binding:"required"`
	FailReason      string `json:"fail_reason"`
	ExpectedVersion int64  `json:"expected_version" binding:"required"`
}

func (h *Handler) updateChecklistItem(c *gin.Context) {
	if _, ok := parseID(c, "id"); !ok {
		return
	}
	if _, ok := parseID(c, "vid"); !ok {
		return
	}
	itemID, ok := parseID(c, "itemId")
	if !ok {
		return
	}
	var req checklistUpdateReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	item, err := h.svc.UpdateChecklistItem(middleware.CurrentUser(c), itemID, service.ChecklistUpdateInput{
		Status: req.Status, FailReason: req.FailReason, ExpectedVersion: req.ExpectedVersion,
	})
	if err != nil {
		writeErr(c, err)
		return
	}
	c.JSON(http.StatusOK, item)
}

type commentReq struct {
	Content string `json:"content" binding:"required"`
}

func (h *Handler) addComment(c *gin.Context) {
	if _, ok := parseID(c, "id"); !ok {
		return
	}
	vid, ok := parseID(c, "vid")
	if !ok {
		return
	}
	var req commentReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	cm, err := h.svc.AddComment(middleware.CurrentUser(c), vid, req.Content)
	if err != nil {
		writeErr(c, err)
		return
	}
	c.JSON(http.StatusCreated, cm)
}

type commentUpdateReq struct {
	Content         string `json:"content" binding:"required"`
	ExpectedVersion int64  `json:"expected_version" binding:"required"`
}

func (h *Handler) updateComment(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	var req commentUpdateReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	cm, err := h.svc.UpdateComment(middleware.CurrentUser(c), id, req.Content, req.ExpectedVersion)
	if err != nil {
		writeErr(c, err)
		return
	}
	c.JSON(http.StatusOK, cm)
}

func (h *Handler) completeReview(c *gin.Context) {
	if _, ok := parseID(c, "id"); !ok {
		return
	}
	vid, ok := parseID(c, "vid")
	if !ok {
		return
	}
	if err := h.svc.CompleteReview(middleware.CurrentUser(c), vid); err != nil {
		writeErr(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"state": "completed"})
}

func (h *Handler) approve(c *gin.Context) {
	jobID, ok := parseID(c, "id")
	if !ok {
		return
	}
	approval, err := h.svc.Approve(middleware.CurrentUser(c), jobID)
	if err != nil {
		writeErr(c, err)
		return
	}
	c.JSON(http.StatusOK, approval)
}

func (h *Handler) report(c *gin.Context) {
	jobID, ok := parseID(c, "id")
	if !ok {
		return
	}
	var revID uint64
	if v := c.Query("revision_id"); v != "" {
		n, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid revision_id"})
			return
		}
		revID = n
	}
	rep, err := h.svc.GetReport(middleware.CurrentUser(c), jobID, revID)
	if err != nil {
		writeErr(c, err)
		return
	}
	c.Header("Content-Disposition", "attachment; filename=report.json")
	c.JSON(http.StatusOK, rep)
}
