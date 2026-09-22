package api

import (
	"fmt"
	"net/http"
	"time"

	"activityguard/internal/audit"
	"activityguard/internal/authctx"
	"activityguard/internal/models"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

type validationError string

func (e validationError) Error() string { return string(e) }

func errField(msg string) error { return validationError(msg) }

func requirePositiveNumbers(p map[string]any, keys ...string) error {
	for _, k := range keys {
		v, ok := p[k]
		if !ok {
			continue // 允许缺省走默认值
		}
		n, ok := v.(float64)
		if !ok || n <= 0 {
			return errField(k + " must be a positive number")
		}
	}
	return nil
}

func requireHourRange(p map[string]any) error {
	check := func(k string, def int) (int, error) {
		v, ok := p[k]
		if !ok {
			return def, nil
		}
		n, ok := v.(float64)
		if !ok || n < 0 || n > 23 || n != float64(int(n)) {
			return 0, errField(k + " must be an integer hour 0-23")
		}
		return int(n), nil
	}
	start, err := check("start_hour", 20)
	if err != nil {
		return err
	}
	end, err := check("end_hour", 6)
	if err != nil {
		return err
	}
	if start == end {
		return errField("start_hour and end_hour must differ")
	}
	return nil
}

func (s *Server) listDepartments(c *gin.Context) {
	ident := c.MustGet("identity").(authctx.Identity)
	q := s.db.Order("name ASC")
	if !ident.IsAdmin() {
		if len(ident.DepartmentIDs) == 0 {
			c.JSON(http.StatusOK, gin.H{"items": []any{}})
			return
		}
		q = q.Where("id IN ?", ident.DepartmentIDs)
	}
	var deps []models.Department
	if err := q.Find(&deps).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "list departments"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": deps})
}

func (s *Server) listEmployees(c *gin.Context) {
	ident := c.MustGet("identity").(authctx.Identity)
	q := s.db.Preload("Department").Order("name ASC")
	if !ident.IsAdmin() {
		if len(ident.DepartmentIDs) == 0 {
			c.JSON(http.StatusOK, gin.H{"items": []any{}})
			return
		}
		q = q.Where("department_id IN ?", ident.DepartmentIDs)
	}
	if d := c.Query("department_id"); d != "" {
		if !ident.CanSeeDepartment(d) {
			c.JSON(http.StatusForbidden, gin.H{"error": "department not assigned to you"})
			return
		}
		q = q.Where("department_id = ?", d)
	}
	var emps []models.Employee
	if err := q.Find(&emps).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "list employees"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": emps})
}

type tzRequest struct {
	Timezone string `json:"timezone" binding:"required"`
}

func (s *Server) updateEmployeeTimezone(c *gin.Context) {
	ident := c.MustGet("identity").(authctx.Identity)
	var req tzRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "timezone is required"})
		return
	}
	if _, err := time.LoadLocation(req.Timezone); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid IANA timezone, e.g. Asia/Shanghai"})
		return
	}

	var emp models.Employee
	if err := s.db.Where("id = ?", c.Param("id")).First(&emp).Error; err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "employee not found"})
		return
	}
	oldTZ := emp.Timezone
	err := s.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Model(&models.Employee{}).Where("id = ?", emp.ID).
			Update("timezone", req.Timezone).Error; err != nil {
			return err
		}
		return audit.InTx(tx, &ident, "employee.update_timezone", "employee", emp.ID, map[string]any{
			"old_timezone": oldTZ, "new_timezone": req.Timezone,
		})
	})
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "update timezone"})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"id": emp.ID, "timezone": req.Timezone,
		"note": fmt.Sprintf("timezone changed %s -> %s; future detection uses the new timezone", oldTZ, req.Timezone),
	})
}

func (s *Server) listAuditLogs(c *gin.Context) {
	q := s.db.Model(&models.AuditLog{}).Order("created_at DESC")
	if a := c.Query("actor_id"); a != "" {
		q = q.Where("actor_id = ?", a)
	}
	if t := c.Query("action"); t != "" {
		q = q.Where("action = ?", t)
	}
	if tt := c.Query("target_type"); tt != "" {
		q = q.Where("target_type = ?", tt)
	}
	limit := 100
	var total int64
	if err := q.Count(&total).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "count audit logs"})
		return
	}
	var logs []models.AuditLog
	if err := q.Limit(limit).Find(&logs).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "list audit logs"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": logs, "total": total, "limit": limit})
}
