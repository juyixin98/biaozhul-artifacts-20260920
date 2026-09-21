// Package api exposes the SiteVitals HTTP API with Gin.
package api

import (
	"errors"
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"sitevitals/internal/models"
	"sitevitals/internal/policy"
	"sitevitals/internal/store"
)

// Server holds HTTP dependencies.
type Server struct {
	Repo  *store.Repo
	Queue *store.Queue
}

// NewServer constructs an API server.
func NewServer(repo *store.Repo, q *store.Queue) *Server {
	return &Server{Repo: repo, Queue: q}
}

// Router builds the Gin engine.
func (s *Server) Router() *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(requestLog())

	api := r.Group("/api/v1")
	{
		api.GET("/health", s.health)

		// Sites & allow-list
		api.POST("/sites", s.createSite)
		api.GET("/sites", s.listSites)
		api.POST("/sites/:id/allowed-urls", s.createAllowedURL)
		api.GET("/sites/:id/allowed-urls", s.listAllowedURLs)

		// Jobs & runs
		api.POST("/jobs", s.createJob)
		api.GET("/jobs", s.listJobs)
		api.GET("/jobs/:id", s.getJob)
		api.GET("/jobs/:id/runs", s.listJobRuns)
		api.GET("/runs/:id", s.getRun)

		// Comparison
		api.GET("/compare", s.compare)

		// Budgets
		api.POST("/budgets", s.createBudget)
		api.GET("/budgets", s.listBudgets)
		api.GET("/budget-evaluations", s.listEvaluations)
	}
	return r
}

func requestLog() gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Next()
	}
}

func (s *Server) health(c *gin.Context) {
	if err := store.Ping(s.Repo.DB); err != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"status": "degraded", "db": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "ok"})
}

// --- Sites -----------------------------------------------------------------

type siteReq struct {
	Name        string `json:"name"`
	SchemeHost  string `json:"scheme_host"`
	Description string `json:"description"`
}

func (s *Server) createSite(c *gin.Context) {
	var req siteReq
	if err := c.ShouldBindJSON(&req); err != nil {
		badRequest(c, "invalid body: "+err.Error())
		return
	}
	if req.Name == "" || req.SchemeHost == "" {
		badRequest(c, "name and scheme_host are required")
		return
	}
	// Only http(s) origins may be registered.
	if _, err := policy.Compile([]models.Site{{ID: 1, SchemeHost: req.SchemeHost, Enabled: true}}, nil); err != nil {
		badRequest(c, err.Error())
		return
	}
	site := &models.Site{Name: req.Name, SchemeHost: req.SchemeHost, Description: req.Description, Enabled: true}
	if err := s.Repo.CreateSite(c.Request.Context(), site); err != nil {
		serverError(c, err)
		return
	}
	c.JSON(http.StatusCreated, site)
}

func (s *Server) listSites(c *gin.Context) {
	sites, err := s.Repo.ListSites(c.Request.Context())
	if err != nil {
		serverError(c, err)
		return
	}
	c.JSON(http.StatusOK, sites)
}

type allowedURLReq struct {
	URLPattern string `json:"url_pattern"`
	Note       string `json:"note"`
}

func (s *Server) createAllowedURL(c *gin.Context) {
	siteID, err := strconv.ParseUint(c.Param("id"), 10, 64)
	if err != nil {
		badRequest(c, "invalid site id")
		return
	}
	var req allowedURLReq
	if err := c.ShouldBindJSON(&req); err != nil {
		badRequest(c, "invalid body: "+err.Error())
		return
	}
	if req.URLPattern == "" {
		badRequest(c, "url_pattern is required")
		return
	}
	sites, err := s.Repo.ListSites(c.Request.Context())
	if err != nil {
		serverError(c, err)
		return
	}
	var site *models.Site
	for i := range sites {
		if sites[i].ID == siteID {
			site = &sites[i]
			break
		}
	}
	if site == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "site not found"})
		return
	}
	if _, err := policy.Compile([]models.Site{*site},
		[]models.AllowedURL{{ID: 1, SiteID: siteID, URLPattern: req.URLPattern, Enabled: true}}); err != nil {
		badRequest(c, err.Error())
		return
	}
	rule := &models.AllowedURL{SiteID: siteID, URLPattern: req.URLPattern, Note: req.Note, Enabled: true}
	if err := s.Repo.CreateAllowedURL(c.Request.Context(), rule); err != nil {
		serverError(c, err)
		return
	}
	c.JSON(http.StatusCreated, rule)
}

func (s *Server) listAllowedURLs(c *gin.Context) {
	siteID, err := strconv.ParseUint(c.Param("id"), 10, 64)
	if err != nil {
		badRequest(c, "invalid site id")
		return
	}
	rules, err := s.Repo.ListAllowedURLs(c.Request.Context(), &siteID)
	if err != nil {
		serverError(c, err)
		return
	}
	c.JSON(http.StatusOK, rules)
}

// --- Jobs ------------------------------------------------------------------

type jobReq struct {
	URL         string `json:"url"`
	Viewport    string `json:"viewport"`
	MaxAttempts int    `json:"max_attempts"`
}

func validViewport(v string) bool {
	return v == models.ViewportMobile || v == models.ViewportTablet || v == models.ViewportDesktop
}

func (s *Server) createJob(c *gin.Context) {
	var req jobReq
	if err := c.ShouldBindJSON(&req); err != nil {
		badRequest(c, "invalid body: "+err.Error())
		return
	}
	if req.URL == "" {
		badRequest(c, "url is required")
		return
	}
	if !validViewport(req.Viewport) {
		badRequest(c, "viewport must be one of: mobile, tablet, desktop")
		return
	}
	sites, rules, err := s.Repo.ActivePolicyData(c.Request.Context())
	if err != nil {
		serverError(c, err)
		return
	}
	checker, err := policy.Compile(sites, rules)
	if err != nil {
		serverError(c, err)
		return
	}
	if v := checker.Check(req.URL); v != nil {
		c.JSON(http.StatusForbidden, gin.H{
			"error": v.Reason,
			"code":  v.Code,
			"url":   v.URL,
		})
		return
	}
	job, err := s.Queue.Enqueue(c.Request.Context(), req.URL, req.Viewport, req.MaxAttempts)
	if err != nil {
		serverError(c, err)
		return
	}
	c.JSON(http.StatusCreated, job)
}

func (s *Server) listJobs(c *gin.Context) {
	limit, _ := strconv.Atoi(c.DefaultQuery("limit", "100"))
	jobs, err := s.Repo.ListJobs(c.Request.Context(), limit)
	if err != nil {
		serverError(c, err)
		return
	}
	c.JSON(http.StatusOK, jobs)
}

func (s *Server) getJob(c *gin.Context) {
	id, err := strconv.ParseUint(c.Param("id"), 10, 64)
	if err != nil {
		badRequest(c, "invalid job id")
		return
	}
	job, err := s.Repo.GetJob(c.Request.Context(), id)
	if err != nil {
		notFound(c, err)
		return
	}
	c.JSON(http.StatusOK, job)
}

func (s *Server) listJobRuns(c *gin.Context) {
	id, err := strconv.ParseUint(c.Param("id"), 10, 64)
	if err != nil {
		badRequest(c, "invalid job id")
		return
	}
	runs, err := s.Repo.ListRunsForJob(c.Request.Context(), id)
	if err != nil {
		serverError(c, err)
		return
	}
	c.JSON(http.StatusOK, runs)
}

func (s *Server) getRun(c *gin.Context) {
	id, err := strconv.ParseUint(c.Param("id"), 10, 64)
	if err != nil {
		badRequest(c, "invalid run id")
		return
	}
	art, err := s.Repo.GetRunArtifacts(c.Request.Context(), id)
	if err != nil {
		notFound(c, err)
		return
	}
	c.JSON(http.StatusOK, art)
}

// --- Comparison ------------------------------------------------------------

// compare?url=...&viewport=...[&run_id=<newer run>] compares the latest two
// SUCCESSFUL runs for the same url+viewport, showing per-metric deltas.
func (s *Server) compare(c *gin.Context) {
	targetURL := c.Query("url")
	viewport := c.Query("viewport")
	if targetURL == "" || !validViewport(viewport) {
		badRequest(c, "url and a valid viewport query parameter are required")
		return
	}
	var newerID uint64
	if v := c.Query("run_id"); v != "" {
		n, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			badRequest(c, "invalid run_id")
			return
		}
		newerID = n
	}
	var newer *models.Run
	if newerID > 0 {
		newer, _ = s.Repo.GetRun(c.Request.Context(), newerID)
		if newer == nil || newer.Status != models.RunSucceeded || newer.TargetURL != targetURL || newer.Viewport != viewport {
			badRequest(c, "run_id is not a successful run for the given url/viewport")
			return
		}
	} else {
		r, err := s.Repo.LatestSucceededRun(c.Request.Context(), targetURL, viewport, 0)
		if err != nil {
			serverError(c, err)
			return
		}
		newer = r
	}
	if newer == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "no successful run found"})
		return
	}
	older, err := s.Repo.LatestSucceededRun(c.Request.Context(), targetURL, viewport, newer.ID)
	if err != nil {
		serverError(c, err)
		return
	}
	if older == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "only one successful run exists; nothing to compare against"})
		return
	}
	newArt, err := s.Repo.GetRunArtifacts(c.Request.Context(), newer.ID)
	if err != nil {
		serverError(c, err)
		return
	}
	oldArt, err := s.Repo.GetRunArtifacts(c.Request.Context(), older.ID)
	if err != nil {
		serverError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"older":  buildRunSummary(oldArt),
		"newer":  buildRunSummary(newArt),
		"deltas": metricDeltas(oldArt.Metrics, newArt.Metrics),
	})
}

// --- Budgets ---------------------------------------------------------------

type budgetReq struct {
	TargetURL    string   `json:"target_url"`
	Viewport     string   `json:"viewport"`
	Metric       string   `json:"metric"`
	ThresholdMS  *float64 `json:"threshold_ms"`
	ThresholdCLS *float64 `json:"threshold_cls"`
}

func (s *Server) createBudget(c *gin.Context) {
	var req budgetReq
	if err := c.ShouldBindJSON(&req); err != nil {
		badRequest(c, "invalid body: "+err.Error())
		return
	}
	if req.TargetURL == "" || req.Metric == "" {
		badRequest(c, "target_url and metric are required")
		return
	}
	if req.Viewport != "" && !validViewport(req.Viewport) {
		badRequest(c, "viewport must be empty (all) or one of mobile/tablet/desktop")
		return
	}
	isCLS := req.Metric == "cls"
	if isCLS {
		if req.ThresholdCLS == nil || *req.ThresholdCLS <= 0 {
			badRequest(c, "cls budget requires threshold_cls > 0")
			return
		}
	} else {
		if req.ThresholdMS == nil || *req.ThresholdMS <= 0 {
			badRequest(c, "millisecond budgets require threshold_ms > 0")
			return
		}
	}
	b := &models.Budget{
		TargetURL: req.TargetURL, Viewport: req.Viewport, Metric: req.Metric,
		ThresholdMS: req.ThresholdMS, ThresholdCLS: req.ThresholdCLS, Enabled: true,
	}
	if err := s.Repo.CreateBudget(c.Request.Context(), b); err != nil {
		serverError(c, err)
		return
	}
	c.JSON(http.StatusCreated, b)
}

func (s *Server) listBudgets(c *gin.Context) {
	var urlPtr *string
	if u := c.Query("url"); u != "" {
		urlPtr = &u
	}
	out, err := s.Repo.ListBudgets(c.Request.Context(), urlPtr)
	if err != nil {
		serverError(c, err)
		return
	}
	c.JSON(http.StatusOK, out)
}

func (s *Server) listEvaluations(c *gin.Context) {
	var runPtr *uint64
	if v := c.Query("run_id"); v != "" {
		n, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			badRequest(c, "invalid run_id")
			return
		}
		runPtr = &n
	}
	limit, _ := strconv.Atoi(c.DefaultQuery("limit", "100"))
	out, err := s.Repo.ListBudgetEvaluations(c.Request.Context(), runPtr, limit)
	if err != nil {
		serverError(c, err)
		return
	}
	c.JSON(http.StatusOK, out)
}

// --- helpers ---------------------------------------------------------------

func badRequest(c *gin.Context, msg string) {
	c.JSON(http.StatusBadRequest, gin.H{"error": msg})
}

func serverError(c *gin.Context, err error) {
	c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
}

func notFound(c *gin.Context, err error) {
	if errors.Is(err, gorm.ErrRecordNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "not found"})
		return
	}
	serverError(c, err)
}
