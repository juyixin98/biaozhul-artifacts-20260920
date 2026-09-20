package api

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/example/forensiccore/internal/config"
	"github.com/example/forensiccore/internal/service"
)

// Router 构建全部 HTTP 路由。
func Router(svc *service.Service) *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(requestLogger())

	h := &authHandler{svc: svc}
	biz := NewHandler(svc)

	r.GET("/health", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"status": "ok"}) })
	r.POST("/api/v1/auth/token", h.issueToken)

	authed := r.Group("/", authRequired(svc.JWTSecret()))
	inv := authed.Group("/", requireRole(config.RoleInvestigator))

	// 案件
	inv.POST("/api/v1/cases", biz.createCase)
	authed.GET("/api/v1/cases", biz.listCases)
	authed.GET("/api/v1/cases/:caseId", biz.getCase)

	// 证据登记（仅调查员）
	inv.POST("/api/v1/cases/:caseId/evidences", biz.registerEvidence)
	authed.GET("/api/v1/cases/:caseId/evidences", biz.listEvidence)
	authed.GET("/api/v1/cases/:caseId/evidences/:evidenceId", biz.getEvidence)

	// 完整性复核（发起：仅调查员；查询：两者）
	inv.POST("/api/v1/cases/:caseId/evidences/:evidenceId/verify", biz.startVerify)
	inv.POST("/api/v1/cases/:caseId/jobs/:jobId/resume", biz.resumeJob)
	authed.GET("/api/v1/cases/:caseId/jobs", biz.listJobs)
	authed.GET("/api/v1/cases/:caseId/jobs/:jobId", biz.getJob)

	// 移交（仅调查员）
	inv.POST("/api/v1/cases/:caseId/evidences/:evidenceId/transfer", biz.transfer)

	// 备注（调查员 + 分析师）
	authed.POST("/api/v1/cases/:caseId/notes", biz.addNote)

	// 证据链查询与校验（两者）
	authed.GET("/api/v1/cases/:caseId/chain", biz.listChain)
	authed.GET("/api/v1/cases/:caseId/chain/verify", biz.verifyChain)

	// 报告导出（两者）
	authed.GET("/api/v1/cases/:caseId/report", biz.exportReport)

	return r
}

func requestLogger() gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Next()
	}
}
