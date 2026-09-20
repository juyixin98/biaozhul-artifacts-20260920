package httpapi

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"

	"proofcycle/internal/service"
	"proofcycle/internal/storage"
)

type Handler struct {
	svc *service.Service
}

func New(svc *service.Service) *Handler { return &Handler{svc: svc} }

// ---- users ----

func (h *Handler) createUser(c *gin.Context) {
	var in service.CreateUserInput
	if err := c.ShouldBindJSON(&in); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"code": "bad_request", "message": err.Error()})
		return
	}
	u, err := h.svc.CreateUser(c.Request.Context(), in)
	if err != nil {
		abortServiceError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{
		"id": u.ID, "name": u.Name, "role": u.Role,
		"api_token": u.APIToken,
		"message":   "save this token now; it is never shown again",
	})
}

// ---- jobs ----

func (h *Handler) createJob(c *gin.Context) {
	var in service.CreateJobInput
	if err := c.ShouldBindJSON(&in); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"code": "bad_request", "message": err.Error()})
		return
	}
	job, err := h.svc.CreateJob(c.Request.Context(), currentUser(c), in)
	if err != nil {
		abortServiceError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{
		"id": job.ID, "name": job.Name, "status": job.Status,
		"designer_id": job.DesignerID, "pm_id": job.PMID,
		"reviewer_ids": in.ReviewerIDs,
	})
}

func (h *Handler) listJobs(c *gin.Context) {
	jobs, err := h.svc.ListJobs(c.Request.Context(), currentUser(c))
	if err != nil {
		abortServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"jobs": jobs})
}

func (h *Handler) getJob(c *gin.Context) {
	jobID, ok := parseID(c, "job_id")
	if !ok {
		return
	}
	detail, err := h.svc.JobDetail(c.Request.Context(), currentUser(c), jobID)
	if err != nil {
		abortServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, detail)
}

// ---- upload / download ----

func (h *Handler) uploadRevision(c *gin.Context) {
	jobID, ok := parseID(c, "job_id")
	if !ok {
		return
	}
	// Reject clearly oversized requests before streaming; exact enforcement is
	// in the storage layer.
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, h.svc.MaxUploadSize()+1)
	fh, err := c.FormFile("file")
	if err != nil {
		var mbErr *http.MaxBytesError
		if errors.As(err, &mbErr) || strings.Contains(err.Error(), "request body too large") {
			abortServiceError(c, &service.Error{
				Status: http.StatusRequestEntityTooLarge, Code: "file_too_large",
				Message: "uploaded file exceeds size limit",
			})
			return
		}
		abortServiceError(c, &service.Error{
			Status: http.StatusBadRequest, Code: "bad_request",
			Message: "multipart form field 'file' is required",
		})
		return
	}
	src, err := fh.Open()
	if err != nil {
		abortServiceError(c, err)
		return
	}
	defer src.Close()

	pr, pw := io.Pipe()
	go func() {
		_, copyErr := io.Copy(pw, src)
		_ = pw.CloseWithError(copyErr)
	}()
	fv, round, err := h.svc.UploadRevision(c.Request.Context(), currentUser(c), jobID, pr)
	if err != nil {
		abortServiceError(c, err)
		return
	}
	if name := c.PostForm("file_name"); name != "" {
		_ = h.svc.SetOriginalName(c.Request.Context(), currentUser(c), fv.ID, name)
	}
	c.JSON(http.StatusCreated, gin.H{
		"job_id": jobID, "version": fv.Version, "version_id": fv.ID,
		"file_name": firstNonEmpty(c.PostForm("file_name"), fh.Filename),
		"sha256":    fv.SHA256, "size_bytes": fv.SizeBytes,
		"mime_type": fv.MimeType, "round_id": round.ID,
	})
}

func (h *Handler) downloadVersion(c *gin.Context) {
	jobID, ok := parseID(c, "job_id")
	if !ok {
		return
	}
	version, err := strconv.Atoi(c.Param("version"))
	if err != nil || version <= 0 {
		c.JSON(http.StatusBadRequest, gin.H{"code": "bad_request", "message": "version must be a positive integer"})
		return
	}
	f, fv, sErr := h.svc.DownloadVersion(c.Request.Context(), currentUser(c), jobID, version)
	if sErr != nil {
		abortServiceError(c, sErr)
		return
	}
	defer f.Close()
	displayName := storage.SafeDownloadName(fv.FileName)
	if displayName == "file" || displayName == "" {
		ext := ".bin"
		if fv.MimeType == storage.MimePDF {
			ext = ".pdf"
		} else if fv.MimeType == storage.MimePNG {
			ext = ".png"
		}
		displayName = fmt.Sprintf("job%d_v%d%s", jobID, version, ext)
	}
	c.DataFromReader(http.StatusOK, fv.SizeBytes, fv.MimeType, f, map[string]string{
		"Content-Disposition": fmt.Sprintf("attachment; filename=%q", displayName),
		"X-SHA-256":           fv.SHA256,
	})
}

// ---- reviews ----

func (h *Handler) submitOpinions(c *gin.Context) {
	jobID, ok := parseID(c, "job_id")
	if !ok {
		return
	}
	var in service.SubmitOpinionsInput
	if err := c.ShouldBindJSON(&in); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"code": "bad_request", "message": err.Error()})
		return
	}
	saved, round, err := h.svc.SubmitOpinions(c.Request.Context(), currentUser(c), jobID, in)
	if err != nil {
		abortServiceError(c, err)
		return
	}
	c.JSON(http.StatusAccepted, gin.H{
		"round_id": round.ID, "version": round.Version, "status": round.Status,
		"updated": len(saved),
	})
}

func (h *Handler) signOff(c *gin.Context) {
	jobID, ok := parseID(c, "job_id")
	if !ok {
		return
	}
	so, state, err := h.svc.SignOff(c.Request.Context(), currentUser(c), jobID)
	if err != nil {
		abortServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"job_id": jobID, "version": so.Version, "approved": true,
		"signoff_at": so.CreatedAt, "basis": so.Basis,
		"reviewers": len(state.Reviewers),
		"checklist": len(state.Items),
	})
}

// ---- history & reports ----

func (h *Handler) versionHistory(c *gin.Context) {
	jobID, ok := parseID(c, "job_id")
	if !ok {
		return
	}
	version, err := strconv.Atoi(c.Param("version"))
	if err != nil || version <= 0 {
		c.JSON(http.StatusBadRequest, gin.H{"code": "bad_request", "message": "version must be a positive integer"})
		return
	}
	view, sErr := h.svc.VersionHistory(c.Request.Context(), currentUser(c), jobID, version)
	if sErr != nil {
		abortServiceError(c, sErr)
		return
	}
	c.JSON(http.StatusOK, view)
}

func (h *Handler) report(c *gin.Context) {
	jobID, ok := parseID(c, "job_id")
	if !ok {
		return
	}
	version, err := strconv.Atoi(c.Param("version"))
	if err != nil || version <= 0 {
		c.JSON(http.StatusBadRequest, gin.H{"code": "bad_request", "message": "version must be a positive integer"})
		return
	}
	markdown := c.GetHeader("Accept") == "text/markdown" || c.Query("format") == "markdown"
	detail, md, sErr := h.svc.Report(c.Request.Context(), currentUser(c), jobID, version, markdown)
	if sErr != nil {
		abortServiceError(c, sErr)
		return
	}
	if markdown {
		c.Header("Content-Disposition",
			fmt.Sprintf("attachment; filename=proofcycle_job%d_v%d_report.md", jobID, version))
		c.Data(http.StatusOK, "text/markdown; charset=utf-8", []byte(md))
		return
	}
	c.JSON(http.StatusOK, gin.H{"job": detail.JobDTO, "report_version": version, "round": roundOf(detail.Rounds, version)})
}

func roundOf(rounds []service.RoundView, version int) service.RoundView {
	for _, r := range rounds {
		if r.Version == version {
			return r
		}
	}
	return service.RoundView{}
}

func parseID(c *gin.Context, key string) (int64, bool) {
	id, err := strconv.ParseInt(c.Param(key), 10, 64)
	if err != nil || id <= 0 {
		c.JSON(http.StatusBadRequest, gin.H{"code": "bad_request", "message": key + " must be a positive integer"})
		return 0, false
	}
	return id, true
}

func firstNonEmpty(a, b string) string {
	if strings.TrimSpace(a) != "" {
		return a
	}
	return b
}
