package httpapi

import (
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"

	"proofcycle/internal/service"
)

// uploadRevision 以 multipart/form-data 上传首版或新修订。
// 字段：file（必填）、sha256（可选，客户端声明摘要）。
func (a *API) uploadRevision(c *gin.Context) {
	me := currentUser(c)
	jobID := c.Param("jobId")

	job, err := a.svc.Job.Get(jobID)
	if err != nil {
		mapServiceError(c, err)
		return
	}
	// 只有作业设计师本人可上传；非成员同样得到 404，避免泄露作业存在性。
	if job.DesignerID != me.ID {
		reviewers, _ := a.svc.Job.Reviewers(jobID)
		if !service.IsMember(job, reviewers, me.ID) {
			fail(c, http.StatusNotFound, "resource not found")
			return
		}
		fail(c, http.StatusForbidden, "%s", service.ErrNotDesigner.Error())
		return
	}

	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, a.maxUpload)
	fh, err := c.FormFile("file")
	if err != nil {
		fail(c, http.StatusBadRequest, "invalid file upload: %s", err.Error())
		return
	}
	src, err := fh.Open()
	if err != nil {
		fail(c, http.StatusInternalServerError, "open upload: %s", err.Error())
		return
	}
	defer src.Close()

	detail, err := a.svc.Version.UploadRevision(service.UploadInput{
		JobID:      jobID,
		UploaderID: me.ID,
		FileName:   fh.Filename,
		Reader:     src,
		ExpectSHA:  c.PostForm("sha256"),
		MaxSize:    a.maxUpload,
	})
	if err != nil {
		mapServiceError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{
		"version_no": detail.VersionNo,
		"version":    detail.Version,
		"snapshot":   detail.Snapshot,
		"items":      detail.Items,
	})
}

func (a *API) listVersions(c *gin.Context) {
	me := currentUser(c)
	jobID := c.Param("jobId")
	if !a.authorizeMember(c, jobID, me.ID) {
		return
	}
	vs, err := a.svc.Version.ListVersions(jobID)
	if err != nil {
		mapServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"versions": vs})
}

func (a *API) getHistory(c *gin.Context) {
	me := currentUser(c)
	jobID := c.Param("jobId")
	if !a.authorizeMember(c, jobID, me.ID) {
		return
	}
	no, ok := parseVersionNo(c)
	if !ok {
		return
	}
	h, err := a.svc.Version.History(jobID, no)
	if err != nil {
		mapServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, h)
}

// downloadVersion 通过服务端存储路径下载文件（路径由服务端生成，接口不接受调用方路径）。
func (a *API) downloadVersion(c *gin.Context) {
	me := currentUser(c)
	jobID := c.Param("jobId")
	if !a.authorizeMember(c, jobID, me.ID) {
		return
	}
	no, ok := parseVersionNo(c)
	if !ok {
		return
	}
	v, err := a.svc.Version.GetVersion(jobID, no)
	if err != nil {
		mapServiceError(c, err)
		return
	}
	f, err := a.svc.Store.Open(v.StoredPath)
	if err != nil {
		mapServiceError(c, err)
		return
	}
	defer f.Close()
	c.Header("Content-Type", v.ContentType)
	c.Header("X-Content-SHA256", v.SHA256)
	c.Header("Content-Disposition", contentDisposition(v.FileName))
	c.Status(http.StatusOK)
	if _, err := copyBody(c.Writer, f); err != nil {
		return
	}
}

// authorizeMember 校验当前用户是作业成员；非成员返回 404（不泄露未授权作业）。
func (a *API) authorizeMember(c *gin.Context, jobID, userID string) bool {
	job, err := a.svc.Job.Get(jobID)
	if err != nil {
		if isNotFound(err) {
			fail(c, http.StatusNotFound, "resource not found")
			return false
		}
		mapServiceError(c, err)
		return false
	}
	reviewers, err := a.svc.Job.Reviewers(jobID)
	if err != nil {
		mapServiceError(c, err)
		return false
	}
	if !service.IsMember(job, reviewers, userID) {
		fail(c, http.StatusNotFound, "resource not found")
		return false
	}
	return true
}

func parseVersionNo(c *gin.Context) (int, bool) {
	no, err := strconv.Atoi(c.Param("versionNo"))
	if err != nil || no <= 0 {
		fail(c, http.StatusBadRequest, "versionNo must be a positive integer")
		return 0, false
	}
	return no, true
}

func isNotFound(err error) bool {
	return err == service.ErrNotFound
}
