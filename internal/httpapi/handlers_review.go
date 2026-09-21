package httpapi

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"proofcycle/internal/domain"
	"proofcycle/internal/service"
)

type itemInputDTO struct {
	SnapshotItemID string `json:"snapshot_item_id" binding:"required"`
	Result         string `json:"result" binding:"required"`
	FailReason     string `json:"fail_reason"`
}

type submitReviewRequest struct {
	VersionNo int            `json:"version_no"` // 0/省略 = 当前版本
	Items     []itemInputDTO `json:"items" binding:"required,min=1"`
}

func toItemInputs(dtos []itemInputDTO) ([]service.ItemInput, bool) {
	items := make([]service.ItemInput, 0, len(dtos))
	for _, d := range dtos {
		r := domain.ItemResult(d.Result)
		if !r.Valid() {
			return nil, false
		}
		items = append(items, service.ItemInput{
			SnapshotItemID: d.SnapshotItemID,
			Result:         r,
			FailReason:     d.FailReason,
		})
	}
	return items, true
}

// POST /jobs/:jobId/reviews  审查员提交（或重新提交）自己的意见。
func (a *API) submitReview(c *gin.Context) {
	me := currentUser(c)
	jobID := c.Param("jobId")
	if !a.authorizeMember(c, jobID, me.ID) {
		return
	}
	var req submitReviewRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		fail(c, http.StatusBadRequest, "invalid request body: %s", err.Error())
		return
	}
	items, ok := toItemInputs(req.Items)
	if !ok {
		fail(c, http.StatusBadRequest, "result must be one of pass, fail, na")
		return
	}
	review, err := a.svc.Review.Submit(service.SubmitReviewInput{
		JobID:     jobID,
		VersionNo: req.VersionNo,
		Reviewer:  me.ID,
		Items:     items,
	})
	if err != nil {
		mapServiceError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"review": review})
}

type updateReviewRequest struct {
	VersionNo       int            `json:"version_no"`
	ExpectedVersion int            `json:"expected_version" binding:"required"`
	Items           []itemInputDTO `json:"items" binding:"required,min=1"`
}

// PUT /jobs/:jobId/reviews  携带预期版本的乐观更新；冲突 -> 409。
func (a *API) updateReview(c *gin.Context) {
	me := currentUser(c)
	jobID := c.Param("jobId")
	if !a.authorizeMember(c, jobID, me.ID) {
		return
	}
	var req updateReviewRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		fail(c, http.StatusBadRequest, "invalid request body: %s", err.Error())
		return
	}
	items, ok := toItemInputs(req.Items)
	if !ok {
		fail(c, http.StatusBadRequest, "result must be one of pass, fail, na")
		return
	}
	review, err := a.svc.Review.Update(service.UpdateReviewInput{
		JobID:           jobID,
		VersionNo:       req.VersionNo,
		Reviewer:        me.ID,
		ExpectedVersion: req.ExpectedVersion,
		Items:           items,
	})
	if err != nil {
		mapServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"review": review})
}

type signOffRequest struct {
	VersionNo int `json:"version_no"` // 0/省略 = 当前版本
}

// POST /jobs/:jobId/approvals  PM 最终签核。
func (a *API) signOff(c *gin.Context) {
	me := currentUser(c)
	jobID := c.Param("jobId")
	if !a.authorizeMember(c, jobID, me.ID) {
		return
	}
	var req signOffRequest
	_ = c.ShouldBindJSON(&req)
	out, err := a.svc.Approval.SignOff(jobID, me.ID, req.VersionNo)
	if err != nil {
		mapServiceError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"approval": out.Approval, "basis": out.Basis})
}

// GET /jobs/:jobId/versions/:versionNo/report?format=json|md
func (a *API) getReport(c *gin.Context) {
	me := currentUser(c)
	jobID := c.Param("jobId")
	if !a.authorizeMember(c, jobID, me.ID) {
		return
	}
	no, ok := parseVersionNo(c)
	if !ok {
		return
	}
	rep, err := a.svc.Report.Build(jobID, no)
	if err != nil {
		mapServiceError(c, err)
		return
	}
	switch c.Query("format") {
	case "md", "markdown":
		c.Header("Content-Type", "text/markdown; charset=utf-8")
		c.String(http.StatusOK, rep.RenderMarkdown())
	default:
		c.JSON(http.StatusOK, rep)
	}
}
