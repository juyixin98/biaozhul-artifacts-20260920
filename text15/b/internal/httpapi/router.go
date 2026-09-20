package httpapi

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"proofcycle/internal/service"
)

// NewEngine builds the Gin engine and registers all routes.
func NewEngine(svc *service.Service, bootstrapToken string) *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Logger(), gin.Recovery())

	r.GET("/healthz", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"status": "ok"})
	})

	api := r.Group("/api/v1")

	// User provisioning: bootstrap only.
	api.POST("/users", BootstrapRequired(bootstrapToken), h(svc).createUser)

	authed := api.Group("")
	authed.Use(AuthRequired(svc))
	{
		jobs := authed.Group("/jobs")
		jobs.POST("", h(svc).createJob)
		jobs.GET("", h(svc).listJobs)
		jobs.GET("/:job_id", h(svc).getJob)
		jobs.POST("/:job_id/revisions", h(svc).uploadRevision)
		jobs.POST("/:job_id/signoff", h(svc).signOff)
		jobs.POST("/:job_id/opinions", h(svc).submitOpinions)
		jobs.GET("/:job_id/versions/:version", h(svc).versionHistory)
		jobs.GET("/:job_id/versions/:version/file", h(svc).downloadVersion)
		jobs.GET("/:job_id/versions/:version/report", h(svc).report)
	}

	return r
}

func h(svc *service.Service) *Handler { return New(svc) }
