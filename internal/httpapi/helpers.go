package httpapi

import (
	"net/http"
	"strconv"
	"time"

	"anomalywatch/internal/admin"
	"anomalywatch/internal/detection"
	"anomalywatch/internal/models"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

// applyEmployeeScope restricts an events query to the caller's department.
// Returns false when the caller has no accessible employees at all.
func (h *Handlers) applyEmployeeScope(c *gin.Context, q *gorm.DB) (*gorm.DB, bool) {
	key := currentKey(c)
	if key.Role == models.RoleAdmin {
		return q, true
	}
	if key.DeptID == nil {
		return q, false
	}
	return q.Where("events.employee_id IN (SELECT id FROM employees WHERE dept_id = ?)", *key.DeptID), true
}

// applyAlertScope restricts an alerts query to the caller's department.
func (h *Handlers) applyAlertScope(c *gin.Context, q *gorm.DB) (*gorm.DB, bool) {
	key := currentKey(c)
	if key.Role == models.RoleAdmin {
		return q, true
	}
	if key.DeptID == nil {
		return q, false
	}
	return q.Where("employees.dept_id = ?", *key.DeptID), true
}

// canAccessEmployee reports whether the caller may view the employee's data.
func (h *Handlers) canAccessEmployee(c *gin.Context, employeeID uint64) bool {
	key := currentKey(c)
	if key.Role == models.RoleAdmin {
		return true
	}
	if key.DeptID == nil {
		return false
	}
	var n int64
	h.DB.Model(&models.Employee{}).
		Where("id = ? AND dept_id = ?", employeeID, *key.DeptID).Count(&n)
	return n > 0
}

// returnPaginated applies limit/offset to q and serializes into dest.
// orderColumn must be qualified (e.g. "alerts.id DESC") so joins do not make
// the bare column ambiguous.
func returnPaginated(c *gin.Context, q *gorm.DB, dest any, orderColumn string) {
	limit := 50
	offset := 0
	if v := c.Query("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 500 {
			limit = n
		}
	}
	if v := c.Query("offset"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			offset = n
		}
	}
	var total int64
	if err := q.Count(&total).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	if err := q.Limit(limit).Offset(offset).Order(orderColumn).Find(dest).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": dest, "total": total, "limit": limit, "offset": offset})
}

// ruleUpdateBody mirrors admin.RuleUpdate for binding.
type ruleUpdateBody struct {
	Name        *string        `json:"name"`
	Description *string        `json:"description"`
	Enabled     *bool          `json:"enabled"`
	Params      models.JSONMap `json:"params"`
}

func updateRuleFromBody(db *gorm.DB, code string, body ruleUpdateBody, actor *models.APIKey, now time.Time) (*models.Rule, error) {
	return admin.UpdateRule(db, code, admin.RuleUpdate{
		Name:        body.Name,
		Description: body.Description,
		Enabled:     body.Enabled,
		Params:      body.Params,
	}, actor, now)
}

func updateEmployeeTimezone(db *gorm.DB, _ *detection.Engine, employeeID uint64, tz string, actor *models.APIKey, now time.Time) (*models.Employee, error) {
	return admin.UpdateTimezone(db, employeeID, tz, actor, now)
}

var _ = http.StatusOK
