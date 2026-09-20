package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"

	"sitevitals/internal/compare"
	"sitevitals/internal/models"
	"sitevitals/internal/store"
	"sitevitals/internal/whitelist"
)

// Server exposes the JSON API.
type Server struct {
	st      *store.Store
	matcher func() *whitelist.Matcher // rebuilt when sites change
}

func NewServer(st *store.Store, matcher func() *whitelist.Matcher) *Server {
	return &Server{st: st, matcher: matcher}
}

// Router builds the gin engine.
func (s *Server) Router() *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(requestLogger())

	r.GET("/healthz", s.health)
	api := r.Group("/api")
	{
		api.GET("/sites", s.listSites)
		api.POST("/sites", s.createSite)
		api.GET("/sites/:id", s.getSite)
		api.PUT("/sites/:id", s.updateSite)
		api.DELETE("/sites/:id", s.deleteSite)

		api.POST("/tasks", s.enqueue)
		api.GET("/tasks", s.listTasks)
		api.GET("/tasks/:id", s.getTask)
		api.POST("/tasks/:id/retry", s.retryTask)
		api.GET("/tasks/:id/runs", s.listRuns)
		api.GET("/tasks/:id/report", s.getReport)

		api.GET("/runs/:id", s.getRun)

		api.POST("/compare", s.compareRuns)
		api.GET("/comparisons", s.listComparisons)

		api.GET("/budgets/global", s.getGlobalBudget)
		api.PUT("/budgets/global", s.updateGlobalBudget)
		api.PUT("/budgets/sites/:id", s.updateSiteBudget)

		api.GET("/alerts", s.listAlerts)
		api.GET("/stats", s.stats)
	}
	return r
}

func requestLogger() gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Next()
	}
}

func (s *Server) health(c *gin.Context) {
	ctx, cancel := context.WithTimeout(c.Request.Context(), 3e9)
	defer cancel()
	sqlDB, err := s.st.DB().DB()
	if err != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"status": "degraded", "error": err.Error()})
		return
	}
	if err := sqlDB.PingContext(ctx); err != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"status": "degraded", "error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "ok"})
}

// ---------- sites ----------

type siteReq struct {
	Name        string `json:"name"`
	Origin      string `json:"origin"`
	PathPrefix  string `json:"path_prefix"`
	Description string `json:"description"`
	Enabled     *bool  `json:"enabled"`
}

func (sr siteReq) toModel() (models.Site, error) {
	site := models.Site{Name: strings.TrimSpace(sr.Name), Description: sr.Description}
	u, err := whitelist.ParseHTTPURL(sr.Origin)
	if err != nil {
		return site, err
	}
	site.Origin = u.Scheme + "://" + u.Host
	prefix := strings.TrimSpace(sr.PathPrefix)
	if prefix == "" {
		prefix = "/"
	}
	if !strings.HasPrefix(prefix, "/") {
		return site, errors.New("path_prefix must start with /")
	}
	site.PathPrefix = prefix
	if sr.Enabled == nil {
		site.Enabled = true
	} else {
		site.Enabled = *sr.Enabled
	}
	if site.Name == "" {
		site.Name = site.Origin + site.PathPrefix
	}
	return site, nil
}

func (s *Server) listSites(c *gin.Context) {
	sites, err := s.st.ListSites(c.Request.Context(), c.Query("all") == "true")
	if err != nil {
		c.JSON(500, gin.H{"error": err.Error()})
		return
	}
	c.JSON(200, gin.H{"sites": sites})
}

func (s *Server) createSite(c *gin.Context) {
	var req siteReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(400, gin.H{"error": err.Error()})
		return
	}
	site, err := req.toModel()
	if err != nil {
		c.JSON(400, gin.H{"error": err.Error()})
		return
	}
	if err := s.st.CreateSite(c.Request.Context(), &site); err != nil {
		c.JSON(409, gin.H{"error": err.Error()})
		return
	}
	c.JSON(201, site)
}

func (s *Server) getSite(c *gin.Context) {
	id, ok := parseID(c)
	if !ok {
		return
	}
	site, err := s.st.GetSite(c.Request.Context(), id)
	if err != nil {
		respondStoreErr(c, err)
		return
	}
	c.JSON(200, site)
}

func (s *Server) updateSite(c *gin.Context) {
	id, ok := parseID(c)
	if !ok {
		return
	}
	var req siteReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(400, gin.H{"error": err.Error()})
		return
	}
	site, err := s.st.GetSite(c.Request.Context(), id)
	if err != nil {
		respondStoreErr(c, err)
		return
	}
	updated, err := req.toModel()
	if err != nil {
		c.JSON(400, gin.H{"error": err.Error()})
		return
	}
	site.Name = updated.Name
	site.Origin = updated.Origin
	site.PathPrefix = updated.PathPrefix
	site.Description = updated.Description
	site.Enabled = updated.Enabled
	if err := s.st.UpdateSite(c.Request.Context(), site); err != nil {
		c.JSON(500, gin.H{"error": err.Error()})
		return
	}
	c.JSON(200, site)
}

func (s *Server) deleteSite(c *gin.Context) {
	id, ok := parseID(c)
	if !ok {
		return
	}
	if err := s.st.DeleteSite(c.Request.Context(), id); err != nil {
		respondStoreErr(c, err)
		return
	}
	c.Status(204)
}

// ---------- tasks ----------

type enqueueReq struct {
	URL         string `json:"url"`
	Viewport    string `json:"viewport"`
	Priority    int    `json:"priority"`
	MaxAttempts int    `json:"max_attempts"`
}

func (s *Server) enqueue(c *gin.Context) {
	var req enqueueReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(400, gin.H{"error": err.Error()})
		return
	}
	vp := models.Viewport(strings.ToLower(strings.TrimSpace(req.Viewport)))
	if !vp.Valid() {
		c.JSON(400, gin.H{"error": "viewport must be one of mobile, tablet, desktop"})
		return
	}
	normalized, err := s.matcher().ValidateTarget(req.URL)
	if err != nil {
		c.JSON(403, gin.H{"error": err.Error()})
		return
	}
	t, err := s.st.Enqueue(c.Request.Context(), store.EnqueueRequest{
		URL: normalized, Viewport: vp, Priority: req.Priority, MaxAttempts: req.MaxAttempts,
	})
	if err != nil {
		c.JSON(500, gin.H{"error": err.Error()})
		return
	}
	c.JSON(201, t)
}

func (s *Server) listTasks(c *gin.Context) {
	limit, _ := strconv.Atoi(c.Query("limit"))
	offset, _ := strconv.Atoi(c.Query("offset"))
	tasks, total, err := s.st.ListTasks(c.Request.Context(), c.Query("status"), limit, offset)
	if err != nil {
		c.JSON(500, gin.H{"error": err.Error()})
		return
	}
	c.JSON(200, gin.H{"tasks": tasks, "total": total})
}

func (s *Server) getTask(c *gin.Context) {
	id, ok := parseID(c)
	if !ok {
		return
	}
	t, err := s.st.GetTask(c.Request.Context(), id)
	if err != nil {
		respondStoreErr(c, err)
		return
	}
	c.JSON(200, t)
}

func (s *Server) retryTask(c *gin.Context) {
	id, ok := parseID(c)
	if !ok {
		return
	}
	t, err := s.st.RetryTask(c.Request.Context(), id)
	if err != nil {
		if errors.Is(err, store.ErrConflict) {
			c.JSON(409, gin.H{"error": err.Error()})
			return
		}
		respondStoreErr(c, err)
		return
	}
	c.JSON(200, t)
}

func (s *Server) listRuns(c *gin.Context) {
	id, ok := parseID(c)
	if !ok {
		return
	}
	runs, err := s.st.ListRunsByTask(c.Request.Context(), id)
	if err != nil {
		c.JSON(500, gin.H{"error": err.Error()})
		return
	}
	c.JSON(200, gin.H{"runs": runs})
}

func (s *Server) getRun(c *gin.Context) {
	id, ok := parseID(c)
	if !ok {
		return
	}
	r, err := s.st.GetRun(c.Request.Context(), id)
	if err != nil {
		respondStoreErr(c, err)
		return
	}
	c.JSON(200, r)
}

func (s *Server) getReport(c *gin.Context) {
	id, ok := parseID(c)
	if !ok {
		return
	}
	r, err := s.st.GetReportByTask(c.Request.Context(), id)
	if err != nil {
		respondStoreErr(c, err)
		return
	}
	c.JSON(200, r)
}

// ---------- compare ----------

type compareReq struct {
	BaselineRunID uint `json:"baseline_run_id"`
	CurrentRunID  uint `json:"current_run_id"`
}

func (s *Server) compareRuns(c *gin.Context) {
	var req compareReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(400, gin.H{"error": err.Error()})
		return
	}
	if req.BaselineRunID == 0 || req.CurrentRunID == 0 {
		c.JSON(400, gin.H{"error": "baseline_run_id and current_run_id are required"})
		return
	}
	base, err := s.st.GetRun(c.Request.Context(), req.BaselineRunID)
	if err != nil {
		respondStoreErr(c, err)
		return
	}
	cur, err := s.st.GetRun(c.Request.Context(), req.CurrentRunID)
	if err != nil {
		respondStoreErr(c, err)
		return
	}
	if err := compare.ValidatePair(base, cur); err != nil {
		c.JSON(409, gin.H{"error": err.Error()})
		return
	}
	d := compare.Diff(base, cur)
	comp := &models.Comparison{
		URL: cur.URL, Viewport: cur.Viewport,
		BaselineRun: base.ID, CurrentRun: cur.ID, Diff: d,
	}
	diffJSON, _ := json.Marshal(d)
	comp.DiffJSON = string(diffJSON)
	if err := s.st.SaveComparison(c.Request.Context(), comp); err != nil {
		c.JSON(500, gin.H{"error": err.Error()})
		return
	}
	c.JSON(200, comp)
}

func (s *Server) listComparisons(c *gin.Context) {
	limit, _ := strconv.Atoi(c.Query("limit"))
	cs, err := s.st.ListComparisons(c.Request.Context(), limit)
	if err != nil {
		c.JSON(500, gin.H{"error": err.Error()})
		return
	}
	c.JSON(200, gin.H{"comparisons": cs})
}

// ---------- budgets / alerts / stats ----------

type budgetReq struct {
	FCPMS           *float64 `json:"fcp_ms"`
	LCPMS           *float64 `json:"lcp_ms"`
	CLS             *float64 `json:"cls"`
	NavDurationMS   *float64 `json:"nav_duration_ms"`
	LongTaskTotalMS *float64 `json:"long_task_total_ms"`
}

func (r budgetReq) toModel(siteID uint) models.Budget {
	return models.Budget{
		SiteID: siteID, FCPMS: r.FCPMS, LCPMS: r.LCPMS, CLS: r.CLS,
		NavDurationMS: r.NavDurationMS, LongTaskTotalMS: r.LongTaskTotalMS,
	}
}

func (s *Server) getGlobalBudget(c *gin.Context) {
	b, err := s.st.EffectiveBudget(c.Request.Context(), 0)
	if err != nil {
		c.JSON(500, gin.H{"error": err.Error()})
		return
	}
	c.JSON(200, b)
}

func (s *Server) updateGlobalBudget(c *gin.Context) {
	var req budgetReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(400, gin.H{"error": err.Error()})
		return
	}
	b := req.toModel(0)
	if err := s.st.UpsertBudget(c.Request.Context(), &b); err != nil {
		c.JSON(500, gin.H{"error": err.Error()})
		return
	}
	c.JSON(200, b)
}

func (s *Server) updateSiteBudget(c *gin.Context) {
	id, ok := parseID(c)
	if !ok {
		return
	}
	if _, err := s.st.GetSite(c.Request.Context(), id); err != nil {
		respondStoreErr(c, err)
		return
	}
	var req budgetReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(400, gin.H{"error": err.Error()})
		return
	}
	b := req.toModel(id)
	if err := s.st.UpsertBudget(c.Request.Context(), &b); err != nil {
		c.JSON(500, gin.H{"error": err.Error()})
		return
	}
	eff, _ := s.st.EffectiveBudget(c.Request.Context(), id)
	c.JSON(200, gin.H{"budget": b, "effective": eff})
}

func (s *Server) listAlerts(c *gin.Context) {
	runID, _ := strconv.ParseUint(c.Query("run_id"), 10, 64)
	limit, _ := strconv.Atoi(c.Query("limit"))
	as, err := s.st.ListAlerts(c.Request.Context(), uint(runID), limit)
	if err != nil {
		c.JSON(500, gin.H{"error": err.Error()})
		return
	}
	c.JSON(200, gin.H{"alerts": as})
}

func (s *Server) stats(c *gin.Context) {
	vp := models.Viewport(c.Query("viewport"))
	if vp != "" && !vp.Valid() {
		c.JSON(400, gin.H{"error": "invalid viewport"})
		return
	}
	normURL := ""
	if raw := c.Query("url"); raw != "" {
		n, err := whitelist.NormalizeRaw(raw)
		if err != nil {
			c.JSON(400, gin.H{"error": err.Error()})
			return
		}
		normURL = n
	}
	st, err := s.st.Stats(c.Request.Context(), normURL, vp)
	if err != nil {
		c.JSON(500, gin.H{"error": err.Error()})
		return
	}
	counts, _ := s.st.TaskCounts(c.Request.Context())
	c.JSON(200, gin.H{"metrics": st, "tasks": counts})
}

// ---------- helpers ----------

func parseID(c *gin.Context) (uint, bool) {
	id, err := strconv.ParseUint(c.Param("id"), 10, 64)
	if err != nil || id == 0 {
		c.JSON(400, gin.H{"error": "invalid id"})
		return 0, false
	}
	return uint(id), true
}

func respondStoreErr(c *gin.Context, err error) {
	switch {
	case errors.Is(err, store.ErrNotFound):
		c.JSON(404, gin.H{"error": "not found"})
	case errors.Is(err, store.ErrConflict):
		c.JSON(409, gin.H{"error": err.Error()})
	default:
		c.JSON(500, gin.H{"error": err.Error()})
	}
}
