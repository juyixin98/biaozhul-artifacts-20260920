package httpapi

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"proofcycle/internal/service"
)

// API 持有服务依赖并注册路由。
type API struct {
	svc       *service.Services
	maxUpload int64
}

// New 创建 API。
func New(svc *service.Services, maxUpload int64) *API {
	return &API{svc: svc, maxUpload: maxUpload}
}

// Handler 构建带路由的 gin engine。
func (a *API) Handler() http.Handler {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(requestLogger())

	r.GET("/healthz", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"status": "ok"}) })

	v1 := r.Group("/api/v1")
	v1.Use(a.authRequired())
	{
		v1.GET("/users", a.listUsers)

		v1.POST("/jobs", a.createJob)
		v1.GET("/jobs", a.listJobs)
		v1.GET("/jobs/:jobId", a.getJob)
		v1.GET("/jobs/:jobId/versions", a.listVersions)
		v1.GET("/jobs/:jobId/versions/:versionNo/history", a.getHistory)
		v1.GET("/jobs/:jobId/versions/:versionNo/report", a.getReport)
		v1.GET("/jobs/:jobId/versions/:versionNo/download", a.downloadVersion)

		v1.POST("/jobs/:jobId/versions", a.uploadRevision)
		v1.POST("/jobs/:jobId/reviews", a.submitReview)
		v1.PUT("/jobs/:jobId/reviews", a.updateReview)
		v1.POST("/jobs/:jobId/approvals", a.signOff)
	}
	return r
}

func requestLogger() gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Next()
	}
}
