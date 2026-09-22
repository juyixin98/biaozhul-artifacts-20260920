package httpapi

import (
	"errors"
	"net/http"
	"strconv"
	"time"

	"anomalywatch/internal/alerts"
	"anomalywatch/internal/config"
	"anomalywatch/internal/detection"
	"anomalywatch/internal/ingest"
	"anomalywatch/internal/models"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

// Handlers bundles service dependencies.
type Handlers struct {
	DB     *gorm.DB
	Cfg    config.Config
	Ingest *ingest.Service
	Engine *detection.Engine
}

// Register wires all routes onto r (routes assume Auth has run).
func (h *Handlers) Register(r gin.IRouter) {
	r.GET("/health", h.health)

	r.POST("/api/v1/events/batch", h.ingestBatch)
	r.GET("/api/v1/events", h.listEvents)

	r.GET("/api/v1/alerts", h.listAlerts)
	r.GET("/api/v1/alerts/:id", h.getAlert)
	r.POST("/api/v1/alerts/:id/transition", h.transitionAlert)

	r.GET("/api/v1/rules", h.listRules)
	r.PUT("/api/v1/rules/:code", RequireRole(models.RoleAdmin), h.updateRule)

	r.GET("/api/v1/departments", h.listDepartments)
	r.GET("/api/v1/employees", h.listEmployees)
	r.PUT("/api/v1/employees/:id/timezone", RequireRole(models.RoleAdmin), h.updateTimezone)

	r.GET("/api/v1/audit", RequireRole(models.RoleAdmin), h.listAudit)

	admin := r.Group("/admin", RequireRole(models.RoleAdmin))
	admin.POST("/jobs/process", h.runProcessJob)
	admin.POST("/jobs/sweep", h.runSweepJob)
	admin.POST("/jobs/escalate", h.runEscalateJob)
}

func (h *Handlers) health(c *gin.Context) {
	sqlDB, err := h.DB.DB()
	if err != nil || sqlDB.Ping() != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"status": "degraded"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "ok", "time": time.Now().UTC()})
}

func (h *Handlers) ingestBatch(c *gin.Context) {
	var req ingest.BatchRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	res, err := h.Ingest.Ingest(&req, time.Now().UTC())
	if err != nil {
		var reject *ingest.RejectError
		if errors.As(err, &reject) {
			c.JSON(http.StatusBadRequest, gin.H{"error": reject.Reason})
			return
		}
		var conflict *ingest.ConflictError
		if errors.As(err, &conflict) {
			c.JSON(http.StatusConflict, gin.H{
				"error":  "one or more events conflict with existing reports",
				"result": conflict.Result,
			})
			return
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusAccepted, res)
}

func (h *Handlers) listEvents(c *gin.Context) {
	q := h.DB.Model(&models.Event{})
	var ok bool
	if q, ok = h.applyEmployeeScope(c, q); !ok {
		c.JSON(http.StatusOK, gin.H{"items": []any{}, "total": 0})
		return
	}
	if empID := c.Query("employee_id"); empID != "" {
		if id, err := strconv.ParseUint(empID, 10, 64); err == nil {
			q = q.Where("events.employee_id = ?", id)
		}
	}
	if et := c.Query("event_type"); et != "" {
		q = q.Where("event_type = ?", et)
	}
	if from := c.Query("from"); from != "" {
		if t, err := time.Parse(time.RFC3339, from); err == nil {
			q = q.Where("occurred_at >= ?", t.UTC())
		}
	}
	if to := c.Query("to"); to != "" {
		if t, err := time.Parse(time.RFC3339, to); err == nil {
			q = q.Where("occurred_at < ?", t.UTC())
		}
	}
	returnPaginated(c, q, &[]models.Event{}, "events.id DESC")
}

func (h *Handlers) listAlerts(c *gin.Context) {
	q := h.DB.Model(&models.Alert{}).
		Joins("JOIN employees ON employees.id = alerts.employee_id")
	var ok bool
	if q, ok = h.applyAlertScope(c, q); !ok {
		c.JSON(http.StatusOK, gin.H{"items": []any{}, "total": 0})
		return
	}
	if st := c.Query("status"); st != "" {
		q = q.Where("alerts.status = ?", st)
	}
	if empID := c.Query("employee_id"); empID != "" {
		if id, err := strconv.ParseUint(empID, 10, 64); err == nil {
			q = q.Where("alerts.employee_id = ?", id)
		}
	}
	if rc := c.Query("rule_code"); rc != "" {
		q = q.Where("alerts.rule_code = ?", rc)
	}
	returnPaginated(c, q, &[]models.Alert{}, "alerts.id DESC")
}

func (h *Handlers) getAlert(c *gin.Context) {
	id, err := strconv.ParseUint(c.Param("id"), 10, 64)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "bad id"})
		return
	}
	var a models.Alert
	if err := h.DB.First(&a, id).Error; err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "alert not found"})
		return
	}
	if !h.canAccessEmployee(c, a.EmployeeID) {
		c.JSON(http.StatusNotFound, gin.H{"error": "alert not found"})
		return
	}
	c.JSON(http.StatusOK, a)
}

type transitionReq struct {
	Status string `json:"status" binding:"required"`
	Note   string `json:"note"`
}

func (h *Handlers) transitionAlert(c *gin.Context) {
	id, err := strconv.ParseUint(c.Param("id"), 10, 64)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "bad id"})
		return
	}
	var req transitionReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	var a models.Alert
	if err := h.DB.First(&a, id).Error; err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "alert not found"})
		return
	}
	if !h.canAccessEmployee(c, a.EmployeeID) {
		c.JSON(http.StatusNotFound, gin.H{"error": "alert not found"})
		return
	}
	out, err := alerts.Transition(h.DB, id, req.Status, req.Note, currentKey(c), time.Now().UTC())
	if err != nil {
		var bad *alerts.InvalidTransitionError
		if errors.As(err, &bad) {
			c.JSON(http.StatusConflict, gin.H{"error": bad.Error()})
			return
		}
		var nf *alerts.NotFoundError
		if errors.As(err, &nf) {
			c.JSON(http.StatusNotFound, gin.H{"error": nf.Error()})
			return
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, out)
}

func (h *Handlers) listRules(c *gin.Context) {
	var rs []models.Rule
	if err := h.DB.Where("active = ?", true).Order("code").Find(&rs).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": rs})
}

func (h *Handlers) updateRule(c *gin.Context) {
	var upd ruleUpdateBody
	if err := c.ShouldBindJSON(&upd); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	code := c.Param("code")
	r, err := updateRuleFromBody(h.DB, code, upd, currentKey(c), time.Now().UTC())
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, r)
}

func (h *Handlers) listDepartments(c *gin.Context) {
	key := currentKey(c)
	q := h.DB.Model(&models.Department{})
	if key.Role == models.RoleAnalyst && key.DeptID != nil {
		q = q.Where("departments.id = ?", *key.DeptID)
	}
	var ds []models.Department
	if err := q.Order("id").Find(&ds).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": ds})
}

func (h *Handlers) listEmployees(c *gin.Context) {
	q := h.DB.Model(&models.Employee{})
	if key := currentKey(c); key.Role == models.RoleAnalyst && key.DeptID != nil {
		q = q.Where("employees.dept_id = ?", *key.DeptID)
	}
	var emps []models.Employee
	if err := q.Order("id").Find(&emps).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": emps})
}

type timezoneReq struct {
	TimeZone string `json:"time_zone" binding:"required"`
}

func (h *Handlers) updateTimezone(c *gin.Context) {
	id, err := strconv.ParseUint(c.Param("id"), 10, 64)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "bad id"})
		return
	}
	var req timezoneReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	emp, err := updateEmployeeTimezone(h.DB, h.Engine, id, req.TimeZone, currentKey(c), time.Now().UTC())
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, emp)
}

func (h *Handlers) listAudit(c *gin.Context) {
	q := h.DB.Model(&models.AuditLog{})
	if action := c.Query("action"); action != "" {
		q = q.Where("action = ?", action)
	}
	if et := c.Query("entity_type"); et != "" {
		q = q.Where("entity_type = ?", et)
	}
	returnPaginated(c, q, &[]models.AuditLog{}, "audit_logs.id DESC")
}

// Admin job triggers — used for deterministic operational control and tests.
func (h *Handlers) runProcessJob(c *gin.Context) {
	n, err := h.Engine.ProcessPending(h.Cfg.ProcessBatch)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"processed": n})
}

func (h *Handlers) runSweepJob(c *gin.Context) {
	ran, err := h.Engine.WithJobLock(c.Request.Context(), "job:sweep", func() error {
		return h.Engine.RunSweep(c.Request.Context())
	})
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"ran": ran})
}

func (h *Handlers) runEscalateJob(c *gin.Context) {
	ran, err := h.Engine.WithJobLock(c.Request.Context(), "job:escalate", func() error {
		_, innerErr := h.Engine.EscalateOverdue()
		return innerErr
	})
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"ran": ran})
}
