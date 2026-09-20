// Package api 提供 ForensicCore 的 HTTP 接口。
//
// 认证为演示用途的预共享头：X-User-Name + X-User-Role（investigator/analyst）。
// 调查员（investigator）可登记证据、发起复核、移交；分析师（analyst）只能查询和备注。
package api

import (
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"forensiccore/internal/chain"
	"forensiccore/internal/evidence"
	"forensiccore/internal/models"
)

// 角色。
const (
	RoleInvestigator = "investigator"
	RoleAnalyst      = "analyst"
)

// Server HTTP 服务。
type Server struct {
	DB    *gorm.DB
	Svc   *evidence.Service
	Chain *chain.Store
}

// NewRouter 构建 Gin 路由。
func NewRouter(db *gorm.DB, svc *evidence.Service, chainStore *chain.Store) *gin.Engine {
	s := &Server{DB: db, Svc: svc, Chain: chainStore}
	r := gin.New()
	r.Use(gin.Recovery())

	api := r.Group("/api", s.authMiddleware())
	// 查询类：两种角色均可
	api.GET("/cases", s.listCases)
	api.GET("/cases/:id", s.getCase)
	api.GET("/cases/:id/evidence", s.listEvidence)
	api.GET("/cases/:id/chain", s.getChain)
	api.GET("/cases/:id/chain/verify", s.verifyChain)
	api.GET("/cases/:id/report", s.exportReport)
	api.GET("/verify-jobs/:id", s.getJob)
	// 备注：调查员与分析师均可
	api.POST("/cases/:id/notes", s.requireRole(RoleInvestigator, RoleAnalyst), s.addNote)
	// 变更类：仅调查员
	api.POST("/cases", s.requireRole(RoleInvestigator), s.createCase)
	api.POST("/cases/:id/evidence", s.requireRole(RoleInvestigator), s.registerEvidence)
	api.POST("/cases/:id/transfers", s.requireRole(RoleInvestigator), s.transfer)
	api.POST("/evidence/:id/verify-jobs", s.requireRole(RoleInvestigator), s.createAndRunJob)
	api.POST("/verify-jobs/:id/resume", s.requireRole(RoleInvestigator), s.resumeJob)
	return r
}

func (s *Server) authMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		user := c.GetHeader("X-User-Name")
		role := c.GetHeader("X-User-Role")
		if user == "" || (role != RoleInvestigator && role != RoleAnalyst) {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"error": "missing or invalid X-User-Name / X-User-Role headers",
			})
			return
		}
		c.Set("user", user)
		c.Set("role", role)
		c.Next()
	}
}

func (s *Server) requireRole(roles ...string) gin.HandlerFunc {
	return func(c *gin.Context) {
		role := c.GetString("role")
		for _, r := range roles {
			if role == r {
				c.Next()
				return
			}
		}
		c.AbortWithStatusJSON(http.StatusForbidden, gin.H{
			"error": "role " + role + " is not allowed to perform this action",
		})
	}
}

func actor(c *gin.Context) string { return c.GetString("user") }

func parseID(c *gin.Context, name string) (uint, bool) {
	id, err := strconv.ParseUint(c.Param(name), 10, 64)
	if err != nil || id == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid " + name})
		return 0, false
	}
	return uint(id), true
}

// ---- 案件 ----

type createCaseReq struct {
	Name        string `json:"name" binding:"required"`
	Description string `json:"description"`
}

func (s *Server) createCase(c *gin.Context) {
	var req createCaseReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	cs := models.Case{Name: req.Name, Description: req.Description}
	if err := s.DB.Create(&cs).Error; err != nil {
		c.JSON(http.StatusConflict, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusCreated, cs)
}

func (s *Server) listCases(c *gin.Context) {
	var cases []models.Case
	if err := s.DB.Find(&cases).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, cases)
}

func (s *Server) getCase(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	var cs models.Case
	if err := s.DB.First(&cs, id).Error; err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "case not found"})
		return
	}
	c.JSON(http.StatusOK, cs)
}

// ---- 证据登记 ----

type registerReq struct {
	Path string `json:"path" binding:"required"` // 相对于证据根目录
}

func (s *Server) registerEvidence(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	var req registerReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	ev, err := s.Svc.Register(id, req.Path, actor(c))
	if err != nil {
		c.JSON(http.StatusUnprocessableEntity, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusCreated, ev)
}

func (s *Server) listEvidence(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	var evs []models.Evidence
	if err := s.DB.Where("case_id = ?", id).Find(&evs).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, evs)
}

// ---- 复核作业 ----

func (s *Server) createAndRunJob(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	job, err := s.Svc.CreateVerifyJob(id)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": err.Error()})
		return
	}
	job, err = s.Svc.RunJob(c.Request.Context(), job.ID, actor(c))
	if err != nil {
		c.JSON(http.StatusConflict, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, job)
}

func (s *Server) resumeJob(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	job, err := s.Svc.RunJob(c.Request.Context(), id, actor(c))
	if err != nil {
		c.JSON(http.StatusConflict, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, job)
}

func (s *Server) getJob(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	job, err := s.Svc.GetJob(id)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "job not found"})
		return
	}
	c.JSON(http.StatusOK, job)
}

// ---- 证据链事件 ----

type noteReq struct {
	Text string `json:"text" binding:"required"`
}

func (s *Server) addNote(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	var req noteReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	ev, err := s.Chain.Append(id, models.EventNote, actor(c), map[string]any{"text": req.Text})
	if err != nil {
		c.JSON(http.StatusUnprocessableEntity, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusCreated, ev)
}

type transferReq struct {
	To   string `json:"to" binding:"required"`
	Note string `json:"note"`
}

func (s *Server) transfer(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	var req transferReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	ev, err := s.Chain.Append(id, models.EventTransfer, actor(c), map[string]any{
		"to":   req.To,
		"note": req.Note,
	})
	if err != nil {
		c.JSON(http.StatusUnprocessableEntity, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusCreated, ev)
}

func (s *Server) getChain(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	var events []models.ChainEvent
	if err := s.DB.Where("case_id = ?", id).Order("seq ASC").Find(&events).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, events)
}

func (s *Server) verifyChain(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	issues, err := s.Chain.Verify(id)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"ok": len(issues) == 0, "issues": issues})
}

// ---- 报告导出 ----

func (s *Server) exportReport(c *gin.Context) {
	id, ok := parseID(c, "id")
	if !ok {
		return
	}
	var cs models.Case
	if err := s.DB.First(&cs, id).Error; err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "case not found"})
		return
	}
	var evs []models.Evidence
	s.DB.Where("case_id = ?", id).Find(&evs)
	var jobs []models.VerifyJob
	s.DB.Where("case_id = ?", id).Find(&jobs)
	var events []models.ChainEvent
	s.DB.Where("case_id = ?", id).Order("seq ASC").Find(&events)
	issues, err := s.Chain.Verify(id)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"case":         cs,
		"generated_at": time.Now().UTC(),
		"baseline":     evs,
		"verify_jobs":  jobs,
		"chain":        events,
		"chain_verification": gin.H{
			"ok":     len(issues) == 0,
			"issues": issues,
		},
		"disclaimer": "哈希链仅提供完整性检查（可发现缺失、篡改、乱序），" +
			"不能代替可信时间戳或外部数字签名；本系统不做磁盘文件系统解析、" +
			"删除恢复，也未取得司法合规认证。",
	})
}
