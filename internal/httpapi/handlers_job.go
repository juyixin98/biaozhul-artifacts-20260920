package httpapi

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"proofcycle/internal/domain"
	"proofcycle/internal/service"
)

func (a *API) listUsers(c *gin.Context) {
	users, err := a.svc.User.List()
	if err != nil {
		mapServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"users": users})
}

type createJobRequest struct {
	Name        string   `json:"name" binding:"required"`
	Description string   `json:"description"`
	DesignerID  string   `json:"designer_id" binding:"required"`
	PMID        string   `json:"pm_id" binding:"required"`
	ReviewerIDs []string `json:"reviewer_ids" binding:"required,min=1,max=8"`
	ChecklistID string   `json:"checklist_id" binding:"required"`
}

func (a *API) createJob(c *gin.Context) {
	me := currentUser(c)
	var req createJobRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		fail(c, http.StatusBadRequest, "invalid request body: %s", err.Error())
		return
	}
	// 只有 PM 角色能创建作业，且必须以自己作为该作业 PM。
	if me.Role != string(domain.RolePM) {
		fail(c, http.StatusForbidden, "only a project manager can create jobs")
		return
	}
	if req.PMID != me.ID {
		fail(c, http.StatusForbidden, "you must assign yourself as the pm of the job")
		return
	}
	job, reviewers, err := a.svc.Job.Create(service.CreateJobInput{
		Name:        req.Name,
		Description: req.Description,
		DesignerID:  req.DesignerID,
		PMID:        req.PMID,
		ReviewerIDs: req.ReviewerIDs,
		ChecklistID: req.ChecklistID,
	})
	if err != nil {
		mapServiceError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"job": job, "reviewers": reviewers})
}

func (a *API) listJobs(c *gin.Context) {
	me := currentUser(c)
	jobs, err := a.svc.Job.ListVisible(me.ID)
	if err != nil {
		mapServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"jobs": jobs})
}

// jobJSON 返回作业详情，含成员角色，便于前端做隔离判断。
func (a *API) getJob(c *gin.Context) {
	me := currentUser(c)
	jobID := c.Param("jobId")

	job, err := a.svc.Job.Get(jobID)
	if err != nil {
		mapServiceError(c, err)
		return
	}
	reviewers, err := a.svc.Job.Reviewers(jobID)
	if err != nil {
		mapServiceError(c, err)
		return
	}
	if !service.IsMember(job, reviewers, me.ID) {
		// 不泄露未授权作业：对非成员返回 404（而非 403）。
		fail(c, http.StatusNotFound, "resource not found")
		return
	}
	c.JSON(http.StatusOK, gin.H{"job": job, "reviewers": reviewers})
}
