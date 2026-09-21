package api

import (
	"net/http"
	"strconv"

	"proofcycle/internal/middleware"
	"proofcycle/internal/models"
	"proofcycle/internal/service"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

func parseID(c *gin.Context, key string) (uint, bool) {
	id, err := strconv.ParseUint(c.Param(key), 10, 64)
	if err != nil || id == 0 {
		fail(c, http.StatusBadRequest, "invalid "+key)
		return 0, false
	}
	return uint(id), true
}

type createJobReq struct {
	Title       string `json:"title"`
	Description string `json:"description"`
	DesignerID  uint   `json:"designer_id"`
	PMID        uint   `json:"pm_id"`
	ReviewerIDs []uint `json:"reviewer_ids"`
}

func (h *Handler) createJob(c *gin.Context) {
	var req createJobReq
	if err := c.ShouldBindJSON(&req); err != nil {
		fail(c, http.StatusBadRequest, "invalid JSON body")
		return
	}
	u := middleware.User(c)

	// Role policy: a designer may open their own job; a PM may open jobs they
	// manage. A reviewer cannot create jobs.
	isActor := func(id uint) bool { return id == u.ID }
	switch {
	case u.Role == models.RoleDesigner && isActor(req.DesignerID):
	case u.Role == models.RolePM && isActor(req.PMID):
	default:
		fail(c, http.StatusForbidden, "designers may only create jobs they design; PMs only jobs they manage")
		return
	}

	job, err := h.svc.CreateJob(service.CreateJobInput{
		Title:       req.Title,
		Description: req.Description,
		DesignerID:  req.DesignerID,
		PMID:        req.PMID,
		ReviewerIDs: req.ReviewerIDs,
	})
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"job": job})
}

func (h *Handler) getJob(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	job, reviewers, found, err := h.svc.GetJob(id)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	if !found {
		fail(c, http.StatusNotFound, "not found")
		return
	}
	actor := middleware.User(c)
	if actor.ID != job.DesignerID && actor.ID != job.PMID && !isRoster(actor.ID, reviewers) {
		fail(c, http.StatusNotFound, "not found") // do not reveal unauthorized jobs
		return
	}
	c.JSON(http.StatusOK, gin.H{"job": jobView(h.db, job, reviewers)})
}

func (h *Handler) listJobs(c *gin.Context) {
	u := middleware.User(c)
	var jobs []models.Job
	q := h.db.Model(&models.Job{}).Order("id DESC").Limit(200)
	switch u.Role {
	case models.RoleDesigner:
		q = q.Where("designer_id = ?", u.ID)
	case models.RolePM:
		q = q.Where("pm_id = ?", u.ID)
	default:
		q = q.Where("id IN (?)",
			h.db.Model(&models.JobReviewer{}).Select("job_id").Where("user_id = ?", u.ID))
	}
	if err := q.Find(&jobs).Error; err != nil {
		fail(c, http.StatusInternalServerError, "failed to list jobs")
		return
	}
	c.JSON(http.StatusOK, gin.H{"jobs": jobs})
}

func isRoster(userID uint, reviewers []models.JobReviewer) bool {
	for _, r := range reviewers {
		if r.UserID == userID {
			return true
		}
	}
	return false
}

// jobView renders a job with preloaded roster users.
func jobView(gdb *gorm.DB, job *models.Job, reviewers []models.JobReviewer) gin.H {
	ids := []uint{job.DesignerID, job.PMID}
	for _, r := range reviewers {
		ids = append(ids, r.UserID)
	}
	var users []models.User
	_ = gdb.Where("id IN ?", ids).Find(&users).Error
	byID := map[uint]models.User{}
	for _, u := range users {
		byID[u.ID] = u
	}
	rv := make([]gin.H, 0, len(reviewers))
	for _, r := range reviewers {
		rv = append(rv, gin.H{"id": r.UserID, "username": byID[r.UserID].Username})
	}
	return gin.H{
		"id":          job.ID,
		"title":       job.Title,
		"description": job.Description,
		"status":      job.Status,
		"designer":    userViewFrom(byID[job.DesignerID]),
		"pm":          userViewFrom(byID[job.PMID]),
		"reviewers":   rv,
		"created_at":  job.CreatedAt,
	}
}

func userViewFrom(u models.User) gin.H {
	if u.ID == 0 {
		return nil
	}
	return gin.H{"id": u.ID, "username": u.Username, "role": u.Role}
}
