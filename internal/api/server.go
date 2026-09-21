// Package api wires HTTP routes to the domain services.
package api

import (
	"net/http"

	"forensiccore/internal/auth"
	"forensiccore/internal/cases"
	"forensiccore/internal/chain"
	"forensiccore/internal/config"
	"forensiccore/internal/evidence"
	"forensiccore/internal/report"
	"forensiccore/internal/review"

	"github.com/gin-gonic/gin"
)

// Services bundles the dependencies of the HTTP layer.
type Services struct {
	Cases    *cases.Service
	Evidence *evidence.Service
	Review   *review.Manager
	Chain    *chain.Appender
	Report   *report.Service
}

// Server holds runtime server state.
type Server struct {
	cfg      config.Config
	services Services
}

// NewServer builds the Gin engine.
func NewServer(cfg config.Config, svcs Services) *gin.Engine {
	s := &Server{cfg: cfg, services: svcs}
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(requestLogger())

	r.GET("/healthz", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"status": "ok"}) })

	v1 := r.Group("/api/v1")

	invOnly := auth.Middleware(cfg.InvestigatorToken, cfg.AnalystToken, auth.RoleInvestigator)
	anyRole := auth.Middleware(cfg.InvestigatorToken, cfg.AnalystToken,
		auth.RoleInvestigator, auth.RoleAnalyst)

	// Cases
	v1.POST("/cases", invOnly, s.createCase)
	v1.GET("/cases", anyRole, s.listCases)
	v1.GET("/cases/:id", anyRole, s.getCase)

	// Evidence
	v1.POST("/cases/:id/evidence", invOnly, s.registerEvidence)
	v1.GET("/cases/:id/evidence", anyRole, s.listEvidence)
	v1.GET("/evidence/:eid", anyRole, s.getEvidence)
	v1.POST("/evidence/:eid/transfer", invOnly, s.transfer)
	v1.POST("/cases/:id/notes", anyRole, s.addNote)

	// Reviews
	v1.POST("/evidence/:eid/reviews", invOnly, s.startReview)
	v1.GET("/cases/:id/reviews", anyRole, s.listReviews)
	v1.GET("/reviews/:rid", anyRole, s.getReview)
	v1.POST("/reviews/:rid/resume", invOnly, s.resumeReview)
	v1.POST("/reviews/:rid/cancel", invOnly, s.cancelReview)

	// Chain
	v1.GET("/cases/:id/chain", anyRole, s.listChain)
	v1.GET("/cases/:id/verify", anyRole, s.verifyChain)

	// Export
	v1.GET("/cases/:id/report", anyRole, s.getReport)

	return r
}
