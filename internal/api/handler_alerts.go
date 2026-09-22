package api

import (
	"net/http"
	"strconv"
	"time"

	"activityguard/internal/audit"
	"activityguard/internal/authctx"
	"activityguard/internal/models"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

// allowedTransitions 定义告警状态机。
var allowedTransitions = map[string]map[string]bool{
	models.AlertNew: {
		models.AlertInvestigating: true,
		models.AlertEscalated:     true,
		models.AlertResolved:      true,
		models.AlertFalsePositive: true,
	},
	models.AlertInvestigating: {
		models.AlertEscalated:     true,
		models.AlertResolved:      true,
		models.AlertFalsePositive: true,
	},
	models.AlertEscalated: {
		models.AlertInvestigating: true,
		models.AlertResolved:      true,
		models.AlertFalsePositive: true,
	},
	models.AlertResolved: {
		models.AlertInvestigating: true, // 误判时可重新打开
	},
	models.AlertFalsePositive: {
		models.AlertInvestigating: true,
	},
}

// terminalStatuses 是人工终态。
func terminalStatus(s string) bool {
	return s == models.AlertResolved || s == models.AlertFalsePositive
}

func (s *Server) listAlerts(c *gin.Context) {
	ident := c.MustGet("identity").(authctx.Identity)

	q := s.db.Model(&models.Alert{})
	if !ident.IsAdmin() {
		if len(ident.DepartmentIDs) == 0 {
			c.JSON(http.StatusOK, gin.H{"items": []any{}, "total": 0, "limit": 0, "offset": 0})
			return
		}
		q = q.Where("department_id IN ?", ident.DepartmentIDs)
	}

	if st := c.Query("status"); st != "" {
		q = q.Where("status = ?", st)
	}
	if rid := c.Query("rule_key"); rid != "" {
		q = q.Where("rule_key = ?", rid)
	}
	if emp := c.Query("employee_id"); emp != "" {
		q = q.Where("employee_id = ?", emp)
	}
	if d := c.Query("department_id"); d != "" {
		if !ident.CanSeeDepartment(d) {
			c.JSON(http.StatusForbidden, gin.H{"error": "department not assigned to you"})
			return
		}
		q = q.Where("department_id = ?", d)
	}

	var total int64
	if err := q.Count(&total).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "count alerts"})
		return
	}

	limit, _ := strconv.Atoi(c.DefaultQuery("limit", "50"))
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	offset, _ := strconv.Atoi(c.DefaultQuery("offset", "0"))
	if offset < 0 {
		offset = 0
	}

	var alerts []models.Alert
	if err := q.Preload("Employee").
		Order("created_at DESC").Limit(limit).Offset(offset).Find(&alerts).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "list alerts"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": alerts, "total": total, "limit": limit, "offset": offset})
}

func (s *Server) getAlert(c *gin.Context) {
	ident := c.MustGet("identity").(authctx.Identity)
	var a models.Alert
	if err := s.db.Preload("Employee").Where("id = ?", c.Param("id")).First(&a).Error; err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "alert not found"})
		return
	}
	// 越权访问统一 404，不暴露资源是否存在。
	if !ident.CanSeeDepartment(a.DepartmentID) {
		c.JSON(http.StatusNotFound, gin.H{"error": "alert not found"})
		return
	}
	c.JSON(http.StatusOK, a)
}

type transitionRequest struct {
	Status string `json:"status" binding:"required"`
	Note   string `json:"note"`
}

func (s *Server) transitionAlert(c *gin.Context) {
	ident := c.MustGet("identity").(authctx.Identity)
	var req transitionRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "status is required"})
		return
	}

	var a models.Alert
	err := s.db.Where("id = ?", c.Param("id")).First(&a).Error
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "alert not found"})
		return
	}
	if !ident.CanSeeDepartment(a.DepartmentID) {
		// 越权返回 404 而非 403，避免泄露告警存在性。
		c.JSON(http.StatusNotFound, gin.H{"error": "alert not found"})
		return
	}
	targets, known := allowedTransitions[a.Status]
	if !known || !targets[req.Status] {
		c.JSON(http.StatusConflict, gin.H{
			"error":        "invalid_status_transition",
			"current":      a.Status,
			"requested":    req.Status,
			"allowed_next": keysOf(targets),
		})
		return
	}

	now := time.Now().UTC()
	updates := map[string]any{"status": req.Status}
	// 首次离开 new 视为已确认；升级也会确认。
	if a.Status == models.AlertNew {
		updates["acknowledged_at"] = now
	}
	assignee := ident.UserID
	updates["assigned_to"] = assignee
	if terminalStatus(req.Status) {
		updates["resolved_at"] = now
	}

	err = s.db.Transaction(func(tx *gorm.DB) error {
		res := tx.Model(&models.Alert{}).Where("id = ? AND status = ?", a.ID, a.Status).Updates(updates)
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			return gorm.ErrRecordNotFound
		}
		return audit.InTx(tx, &ident, "alert.transition", "alert", a.ID, map[string]any{
			"from": a.Status, "to": req.Status, "note": req.Note,
			"rule_key": a.RuleKey, "dedup_key": a.DedupKey,
		})
	})
	if err != nil {
		if err == gorm.ErrRecordNotFound {
			c.JSON(http.StatusConflict, gin.H{"error": "alert changed concurrently, retry"})
			return
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": "transition failed"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"id": a.ID, "status": req.Status})
}

func keysOf(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
