package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"

	"forensiccore/internal/auth"
	"forensiccore/internal/config"
	"forensiccore/internal/jobs"
	"forensiccore/internal/models"
	"forensiccore/internal/report"
	"forensiccore/internal/safeio"
	"forensiccore/internal/service"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

// Server wires the HTTP layer.
type Server struct {
	DB      *gorm.DB
	Service *service.Service
	Runner  *jobs.Runner
	Cfg     *config.Config
}

// NewRouter builds the gin engine with all routes.
func (s *Server) NewRouter() *gin.Engine {
	r := gin.New()
	r.Use(gin.Recovery())
	r.GET("/healthz", func(c *gin.Context) {
		sqlDB, err := s.DB.DB()
		if err != nil || sqlDB.Ping() != nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"status": "db unavailable"})
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "ok"})
	})

	v1 := r.Group("/api/v1")
	v1.Use(auth.Middleware(s.Cfg.Principals))
	{
		inv := v1.Group("", auth.RequireRole(config.RoleInvestigator))
		inv.POST("/cases", s.registerCase)
		inv.POST("/cases/:id/transfers", s.transfer)
		inv.POST("/cases/:id/verifications", s.startVerification)
		inv.POST("/cases/:id/verifications/:jobID/resume", s.resumeVerification)

		any := v1.Group("", auth.RequireRole(config.RoleInvestigator, config.RoleAnalyst))
		any.GET("/cases", s.listCases)
		any.GET("/cases/:id", s.getCase)
		any.GET("/cases/:id/events", s.listEvents)
		any.GET("/cases/:id/verifications", s.listJobs)
		any.GET("/cases/:id/verify", s.verifyChain)
		any.POST("/cases/:id/notes", s.addNote)
		any.GET("/cases/:id/export", s.exportCase)
	}
	return r
}

func parseID(c *gin.Context, key string) (uint, bool) {
	id, err := strconv.ParseUint(c.Param(key), 10, 64)
	if err != nil || id == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid " + key})
		return 0, false
	}
	return uint(id), true
}

func fail(c *gin.Context, err error) {
	switch {
	case errors.Is(err, gorm.ErrRecordNotFound):
		c.JSON(http.StatusNotFound, gin.H{"error": "not found"})
	case errors.Is(err, safeio.ErrOutsideWhitelist), errors.Is(err, safeio.ErrIllegalName):
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
	case errors.Is(err, safeio.ErrNotRegular):
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
	case errors.Is(err, safeio.ErrFileChanged):
		c.JSON(http.StatusConflict, gin.H{"error": err.Error(), "code": "FILE_CHANGED"})
	case errors.Is(err, service.ErrConflict):
		c.JSON(http.StatusConflict, gin.H{"error": err.Error()})
	default:
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
	}
}

func (s *Server) registerCase(c *gin.Context) {
	var req service.RegisterRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid JSON body"})
		return
	}
	created, err := s.Service.RegisterCase(req, auth.Principal(c).Name)
	if err != nil {
		fail(c, err)
		return
	}
	c.JSON(http.StatusCreated, created)
}

func (s *Server) listCases(c *gin.Context) {
	cs, err := s.Service.ListCases()
	if err != nil {
		fail(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"cases": cs})
}

func (s *Server) getCase(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	cs, err := s.Service.GetCase(id)
	if err != nil {
		fail(c, err)
		return
	}
	c.JSON(http.StatusOK, cs)
}

func (s *Server) transfer(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	var req service.TransferRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid JSON body"})
		return
	}
	ev, err := s.Service.Transfer(id, req, auth.Principal(c).Name)
	if err != nil {
		fail(c, err)
		return
	}
	c.JSON(http.StatusCreated, presentEvent(ev))
}

func (s *Server) addNote(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	var body struct {
		Body string `json:"body"`
	}
	if err := c.ShouldBindJSON(&body); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid JSON body"})
		return
	}
	ev, err := s.Service.Note(id, body.Body, auth.Principal(c).Name)
	if err != nil {
		fail(c, err)
		return
	}
	c.JSON(http.StatusCreated, presentEvent(ev))
}

func (s *Server) startVerification(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	var body struct {
		ChunkSize int `json:"chunk_size"`
	}
	_ = c.ShouldBindJSON(&body)
	job, err := s.Service.StartVerification(id, auth.Principal(c).Name, body.ChunkSize)
	if err != nil {
		fail(c, err)
		return
	}
	c.JSON(http.StatusCreated, job)
}

func (s *Server) resumeVerification(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	jobID, ok := parseID(c, "jobID")
	if !ok {
		return
	}
	job, err := jobs.Resume(s.DB, id, jobID)
	if err != nil {
		fail(c, err)
		return
	}
	c.JSON(http.StatusOK, job)
}

func (s *Server) listJobs(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	js, err := s.Service.ListJobs(id)
	if err != nil {
		fail(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"jobs": js})
}

func (s *Server) listEvents(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	events, err := s.Service.ListEvents(id)
	if err != nil {
		fail(c, err)
		return
	}
	out := make([]gin.H, 0, len(events))
	for i := range events {
		out = append(out, presentEvent(&events[i]))
	}
	c.JSON(http.StatusOK, gin.H{"events": out})
}

func (s *Server) verifyChain(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	res, err := s.Service.VerifyChain(id)
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			fail(c, err)
			return
		}
		c.JSON(http.StatusOK, gin.H{"ok": false, "error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, res)
}

func (s *Server) exportCase(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	rep, err := report.Build(s.DB, id)
	if err != nil {
		fail(c, err)
		return
	}
	c.JSON(http.StatusOK, rep)
}

func presentEvent(e *models.ChainEvent) gin.H {
	var payload any
	if len(e.Payload) > 0 {
		_ = json.Unmarshal(e.Payload, &payload)
	}
	return gin.H{
		"seq":            e.Seq,
		"type":           e.Type,
		"actor":          e.Actor,
		"payload":        payload,
		"prev_digest":    e.PrevDigest,
		"content_digest": e.ContentDigest,
		"entry_digest":   e.EntryDigest,
		"created_at":     e.CreatedAt,
	}
}
