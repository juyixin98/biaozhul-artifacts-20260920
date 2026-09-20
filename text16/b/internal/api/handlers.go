package api

import (
	"errors"
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"

	"github.com/example/forensiccore/internal/service"
)

// Handler 挂载业务路由。
type Handler struct {
	svc *service.Service
}

// NewHandler 创建 API 处理器。
func NewHandler(svc *service.Service) *Handler { return &Handler{svc: svc} }

type createCaseRequest struct {
	CaseNumber string `json:"case_number"`
	Title      string `json:"title"`
	Custodian  string `json:"custodian"`
}

func (h *Handler) createCase(c *gin.Context) {
	user, _ := currentUser(c)
	var req createCaseRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid json body"})
		return
	}
	cs, err := h.svc.CreateCase(service.CreateCaseInput{
		CaseNumber: req.CaseNumber,
		Title:      req.Title,
		Custodian:  req.Custodian,
		Actor:      user,
	})
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusCreated, cs)
}

func (h *Handler) listCases(c *gin.Context) {
	cs, err := h.svc.ListCases()
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"cases": cs})
}

func (h *Handler) getCase(c *gin.Context) {
	id, ok := parseUintParam(c, "caseId")
	if !ok {
		return
	}
	cs, err := h.svc.GetCase(id)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, cs)
}

type registerEvidenceRequest struct {
	RootName string `json:"root_name"`
	RelPath  string `json:"rel_path"`
	Name     string `json:"name"`
}

func (h *Handler) registerEvidence(c *gin.Context) {
	caseID, ok := parseUintParam(c, "caseId")
	if !ok {
		return
	}
	user, _ := currentUser(c)
	var req registerEvidenceRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid json body"})
		return
	}
	ev, err := h.svc.RegisterEvidence(service.RegisterEvidenceInput{
		CaseID:   caseID,
		RootName: req.RootName,
		RelPath:  req.RelPath,
		Name:     req.Name,
		Actor:    user,
	})
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusCreated, ev)
}

func (h *Handler) listEvidence(c *gin.Context) {
	caseID, ok := parseUintParam(c, "caseId")
	if !ok {
		return
	}
	evs, err := h.svc.ListEvidence(caseID)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"evidences": evs})
}

func (h *Handler) getEvidence(c *gin.Context) {
	caseID, ok := parseUintParam(c, "caseId")
	if !ok {
		return
	}
	evID, ok := parseUintParam(c, "evidenceId")
	if !ok {
		return
	}
	ev, err := h.svc.GetEvidence(caseID, evID)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, ev)
}

func (h *Handler) startVerify(c *gin.Context) {
	caseID, ok := parseUintParam(c, "caseId")
	if !ok {
		return
	}
	evID, ok := parseUintParam(c, "evidenceId")
	if !ok {
		return
	}
	user, _ := currentUser(c)
	job, err := h.svc.StartVerify(caseID, evID, user)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusAccepted, job)
}

func (h *Handler) resumeJob(c *gin.Context) {
	caseID, ok := parseUintParam(c, "caseId")
	if !ok {
		return
	}
	jobID, ok := parseUintParam(c, "jobId")
	if !ok {
		return
	}
	job, err := h.svc.ResumeJob(caseID, jobID)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusAccepted, job)
}

func (h *Handler) listJobs(c *gin.Context) {
	caseID, ok := parseUintParam(c, "caseId")
	if !ok {
		return
	}
	jobs, err := h.svc.ListJobs(caseID)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"jobs": jobs})
}

func (h *Handler) getJob(c *gin.Context) {
	caseID, ok := parseUintParam(c, "caseId")
	if !ok {
		return
	}
	jobID, ok := parseUintParam(c, "jobId")
	if !ok {
		return
	}
	job, chunks, err := h.svc.GetJob(caseID, jobID)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"job": job, "chunks": chunks})
}

type transferRequest struct {
	To     string `json:"to"`
	Reason string `json:"reason"`
}

func (h *Handler) transfer(c *gin.Context) {
	caseID, ok := parseUintParam(c, "caseId")
	if !ok {
		return
	}
	evID, ok := parseUintParam(c, "evidenceId")
	if !ok {
		return
	}
	user, _ := currentUser(c)
	var req transferRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid json body"})
		return
	}
	ev, err := h.svc.Transfer(service.TransferInput{
		CaseID: caseID, EvidenceID: evID, To: req.To,
		Reason: req.Reason, Actor: user,
	})
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusCreated, ev)
}

type noteRequest struct {
	EvidenceID uint   `json:"evidence_id"`
	Text       string `json:"text"`
}

func (h *Handler) addNote(c *gin.Context) {
	caseID, ok := parseUintParam(c, "caseId")
	if !ok {
		return
	}
	user, _ := currentUser(c)
	var req noteRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid json body"})
		return
	}
	ev, err := h.svc.AddNote(service.NoteInput{
		CaseID: caseID, EvidenceID: req.EvidenceID,
		Text: req.Text, Actor: user,
	})
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusCreated, ev)
}

func (h *Handler) listChain(c *gin.Context) {
	caseID, ok := parseUintParam(c, "caseId")
	if !ok {
		return
	}
	events, err := h.svc.ListChain(caseID)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"events": events})
}

func (h *Handler) verifyChain(c *gin.Context) {
	caseID, ok := parseUintParam(c, "caseId")
	if !ok {
		return
	}
	rep, err := h.svc.VerifyChain(caseID)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	c.JSON(http.StatusOK, rep)
}

func (h *Handler) exportReport(c *gin.Context) {
	caseID, ok := parseUintParam(c, "caseId")
	if !ok {
		return
	}
	rep, err := h.svc.BuildReport(caseID)
	if err != nil {
		writeServiceError(c, err)
		return
	}
	format := c.DefaultQuery("format", "json")
	switch format {
	case "markdown", "md":
		b, err := rep.RenderMarkdown()
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		c.Header("Content-Type", "text/markdown; charset=utf-8")
		c.String(http.StatusOK, string(b))
	default:
		c.JSON(http.StatusOK, rep)
	}
}

func parseUintParam(c *gin.Context, key string) (uint, bool) {
	v, err := strconv.ParseUint(c.Param(key), 10, 64)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid " + key})
		return 0, false
	}
	return uint(v), true
}

func writeServiceError(c *gin.Context, err error) {
	var se *service.Error
	if errors.As(err, &se) {
		status := http.StatusInternalServerError
		switch se.Kind {
		case service.KindValidation:
			status = http.StatusBadRequest
		case service.KindUnauthorized:
			status = http.StatusUnauthorized
		case service.KindForbidden:
			status = http.StatusForbidden
		case service.KindNotFound:
			status = http.StatusNotFound
		case service.KindConflict, service.KindFileChanged:
			status = http.StatusConflict
		}
		c.JSON(status, gin.H{"error": se.Msg, "kind": string(se.Kind)})
		return
	}
	c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
}
