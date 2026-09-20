// Package httpapi exposes the DeskLens REST API over Echo.
package httpapi

import (
	"database/sql"
	"encoding/csv"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/labstack/echo/v4"

	"desklens/internal/classify"
	"desklens/internal/model"
	"desklens/internal/store"
)

type Handler struct {
	st *store.Store
}

func NewHandler(st *store.Store) *Handler { return &Handler{st: st} }

// Router builds the Echo instance with all routes and middleware.
func Router(st *store.Store) *echo.Echo {
	h := NewHandler(st)
	e := echo.New()
	e.HideBanner = true
	e.HTTPErrorHandler = errorHandler

	e.GET("/healthz", func(c echo.Context) error { return c.String(http.StatusOK, "ok") })

	api := e.Group("/api/v1", h.auth)

	api.POST("/snapshots/batch", h.ingestBatch, requireRole("ingest", "admin"))

	api.GET("/employees/:id/daily", h.employeeDaily, requireRole("admin", "manager"))
	api.GET("/employees/:id/snapshots", h.employeeSnapshots, requireRole("admin", "manager"))
	api.GET("/departments/:id/weekly", h.departmentWeekly, requireRole("admin", "manager"))
	api.GET("/export/employees/:id/daily.csv", h.exportEmployeeDaily, requireRole("admin", "manager"))
	api.GET("/export/departments/:id/weekly.csv", h.exportDepartmentWeekly, requireRole("admin", "manager"))

	admin := api.Group("/admin", requireRole("admin"))
	admin.POST("/policies", h.publishPolicy)
	admin.POST("/classifications", h.publishClassification)
	admin.POST("/rebuild", h.rebuild)
	admin.POST("/cleanup", h.cleanup)

	return e
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

type ctxKey string

const userKey ctxKey = "user"

func (h *Handler) auth(next echo.HandlerFunc) echo.HandlerFunc {
	return func(c echo.Context) error {
		header := c.Request().Header.Get("Authorization")
		token := strings.TrimSpace(strings.TrimPrefix(header, "Bearer "))
		if token == "" {
			return echo.NewHTTPError(http.StatusUnauthorized, "missing bearer token")
		}
		u, err := h.st.UserByToken(c.Request().Context(), token)
		if errors.Is(err, sql.ErrNoRows) {
			return echo.NewHTTPError(http.StatusUnauthorized, "invalid token")
		}
		if err != nil {
			return err
		}
		c.Set(string(userKey), u)
		return next(c)
	}
}

func requireRole(roles ...string) echo.MiddlewareFunc {
	allowed := map[string]bool{}
	for _, r := range roles {
		allowed[r] = true
	}
	return func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			u := currentUser(c)
			if u == nil || !allowed[u.Role] {
				return echo.NewHTTPError(http.StatusForbidden, "insufficient role")
			}
			return next(c)
		}
	}
}

func currentUser(c echo.Context) *model.User {
	u, _ := c.Get(string(userKey)).(*model.User)
	return u
}

// scopeEmployee enforces department isolation: a manager may only reach
// employees of their own department; admins may reach everyone.
func (h *Handler) scopeEmployee(c echo.Context, empID int64) (*model.Employee, error) {
	emp, err := h.st.GetEmployee(c.Request().Context(), empID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, echo.NewHTTPError(http.StatusNotFound, "employee not found")
	}
	if err != nil {
		return nil, err
	}
	u := currentUser(c)
	if u.Role == "manager" && (u.DepartmentID == nil || *u.DepartmentID != emp.DepartmentID) {
		return nil, echo.NewHTTPError(http.StatusForbidden, "employee is outside your department")
	}
	return emp, nil
}

// scopeDepartment enforces the same isolation for department-level reads.
func (h *Handler) scopeDepartment(c echo.Context, deptID int64) error {
	u := currentUser(c)
	if u.Role == "manager" && (u.DepartmentID == nil || *u.DepartmentID != deptID) {
		return echo.NewHTTPError(http.StatusForbidden, "department is outside your scope")
	}
	return nil
}

// ---------------------------------------------------------------------------
// Ingestion
// ---------------------------------------------------------------------------

func (h *Handler) ingestBatch(c echo.Context) error {
	var body struct {
		Snapshots []model.SnapshotInput `json:"snapshots"`
	}
	if err := c.Bind(&body); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid JSON body")
	}
	res, err := h.st.IngestBatch(c.Request().Context(), body.Snapshots)
	if err != nil {
		return err // mapped by errorHandler
	}
	return c.JSON(http.StatusOK, res)
}

// ---------------------------------------------------------------------------
// Read endpoints
// ---------------------------------------------------------------------------

func parseDate(c echo.Context, name string) (time.Time, error) {
	s := c.QueryParam(name)
	if s == "" {
		return time.Time{}, echo.NewHTTPError(http.StatusBadRequest, "missing query parameter: "+name)
	}
	t, err := time.Parse("2006-01-02", s)
	if err != nil {
		return time.Time{}, echo.NewHTTPError(http.StatusBadRequest, name+" must be YYYY-MM-DD")
	}
	return t, nil
}

func parseRange(c echo.Context) (time.Time, time.Time, error) {
	from, err := parseDate(c, "from")
	if err != nil {
		return time.Time{}, time.Time{}, err
	}
	to, err := parseDate(c, "to")
	if err != nil {
		return time.Time{}, time.Time{}, err
	}
	if to.Before(from) {
		return time.Time{}, time.Time{}, echo.NewHTTPError(http.StatusBadRequest, "'to' must not be before 'from'")
	}
	return from, to, nil
}

func pathID(c echo.Context, name string) (int64, error) {
	id, err := strconv.ParseInt(c.Param(name), 10, 64)
	if err != nil || id <= 0 {
		return 0, echo.NewHTTPError(http.StatusBadRequest, "invalid "+name)
	}
	return id, nil
}

func (h *Handler) employeeDaily(c echo.Context) error {
	empID, err := pathID(c, "id")
	if err != nil {
		return err
	}
	if _, err := h.scopeEmployee(c, empID); err != nil {
		return err
	}
	from, to, err := parseRange(c)
	if err != nil {
		return err
	}
	rows, err := h.st.DailySummaries(c.Request().Context(), empID, from, to)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"employee_id": empID, "days": rows})
}

func (h *Handler) employeeSnapshots(c echo.Context) error {
	empID, err := pathID(c, "id")
	if err != nil {
		return err
	}
	emp, err := h.scopeEmployee(c, empID)
	if err != nil {
		return err
	}
	day, err := parseDate(c, "date")
	if err != nil {
		return err
	}
	rows, err := h.st.SnapshotsForDay(c.Request().Context(), empID, emp.Timezone, day)
	if err != nil {
		return err
	}
	cutoff, err := h.st.RetentionCutoff(c.Request().Context())
	if err != nil {
		return err
	}
	resp := map[string]any{"employee_id": empID, "date": day.Format("2006-01-02"), "snapshots": rows}
	if cutoff != nil && day.Before(*cutoff) {
		resp["detail_available_from"] = cutoff.Format(time.RFC3339)
		resp["note"] = "raw detail before the retention cutoff has been cleaned up; summaries remain available"
	}
	return c.JSON(http.StatusOK, resp)
}

func (h *Handler) departmentWeekly(c echo.Context) error {
	deptID, err := pathID(c, "id")
	if err != nil {
		return err
	}
	if err := h.scopeDepartment(c, deptID); err != nil {
		return err
	}
	from, to, err := parseRange(c)
	if err != nil {
		return err
	}
	rows, err := h.st.WeeklySummaries(c.Request().Context(), deptID, from, to)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"department_id": deptID, "weeks": rows})
}

// ---------------------------------------------------------------------------
// CSV export (same department isolation as the JSON endpoints)
// ---------------------------------------------------------------------------

func (h *Handler) exportEmployeeDaily(c echo.Context) error {
	empID, err := pathID(c, "id")
	if err != nil {
		return err
	}
	if _, err := h.scopeEmployee(c, empID); err != nil {
		return err
	}
	from, to, err := parseRange(c)
	if err != nil {
		return err
	}
	rows, err := h.st.DailySummaries(c.Request().Context(), empID, from, to)
	if err != nil {
		return err
	}
	c.Response().Header().Set(echo.HeaderContentType, "text/csv")
	c.Response().Header().Set(echo.HeaderContentDisposition,
		fmt.Sprintf("attachment; filename=employee-%d-daily.csv", empID))
	w := csv.NewWriter(c.Response())
	w.Write([]string{"employee_id", "date", "productive", "unproductive", "neutral", "total"})
	for _, r := range rows {
		w.Write([]string{strconv.FormatInt(r.EmployeeID, 10), r.Day.Format("2006-01-02"),
			strconv.FormatInt(r.Productive, 10), strconv.FormatInt(r.Unproductive, 10),
			strconv.FormatInt(r.Neutral, 10), strconv.FormatInt(r.Total, 10)})
	}
	w.Flush()
	return nil
}

func (h *Handler) exportDepartmentWeekly(c echo.Context) error {
	deptID, err := pathID(c, "id")
	if err != nil {
		return err
	}
	if err := h.scopeDepartment(c, deptID); err != nil {
		return err
	}
	from, to, err := parseRange(c)
	if err != nil {
		return err
	}
	rows, err := h.st.WeeklySummaries(c.Request().Context(), deptID, from, to)
	if err != nil {
		return err
	}
	c.Response().Header().Set(echo.HeaderContentType, "text/csv")
	c.Response().Header().Set(echo.HeaderContentDisposition,
		fmt.Sprintf("attachment; filename=department-%d-weekly.csv", deptID))
	w := csv.NewWriter(c.Response())
	w.Write([]string{"department_id", "week_start", "productive", "unproductive", "neutral", "total"})
	for _, r := range rows {
		w.Write([]string{strconv.FormatInt(r.DepartmentID, 10), r.WeekStart.Format("2006-01-02"),
			strconv.FormatInt(r.Productive, 10), strconv.FormatInt(r.Unproductive, 10),
			strconv.FormatInt(r.Neutral, 10), strconv.FormatInt(r.Total, 10)})
	}
	w.Flush()
	return nil
}

// ---------------------------------------------------------------------------
// Admin endpoints
// ---------------------------------------------------------------------------

func (h *Handler) publishPolicy(c echo.Context) error {
	var body struct {
		WorkStart           string   `json:"work_start"` // "HH:MM" local
		WorkEnd             string   `json:"work_end"`
		Workdays            []int64  `json:"workdays"`
		ExcludedApps        []string `json:"excluded_apps"`
		ExemptDepartmentIDs []int64  `json:"exempt_department_ids"`
	}
	if err := c.Bind(&body); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid JSON body")
	}
	start, err := parseHHMM(body.WorkStart)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "work_start must be HH:MM")
	}
	end, err := parseHHMM(body.WorkEnd)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "work_end must be HH:MM")
	}
	ver, err := h.st.PublishPolicy(c.Request().Context(), start, end,
		body.Workdays, body.ExcludedApps, body.ExemptDepartmentIDs)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, map[string]any{"policy_version": ver})
}

func parseHHMM(s string) (int, error) {
	t, err := time.Parse("15:04", s)
	if err != nil {
		return 0, err
	}
	return t.Hour()*60 + t.Minute(), nil
}

func (h *Handler) publishClassification(c echo.Context) error {
	var body struct {
		Rules []struct {
			Pattern  string `json:"pattern"`
			Category string `json:"category"`
			Priority int    `json:"priority"`
		} `json:"rules"`
	}
	if err := c.Bind(&body); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid JSON body")
	}
	rules := make([]classify.Rule, len(body.Rules))
	for i, r := range body.Rules {
		rules[i] = classify.Rule{Pattern: r.Pattern, Category: r.Category, Priority: r.Priority}
	}
	ver, err := h.st.PublishClassification(c.Request().Context(), rules)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, map[string]any{"classification_version": ver})
}

func (h *Handler) rebuild(c echo.Context) error {
	var body struct {
		From string `json:"from"`
		To   string `json:"to"`
	}
	if err := c.Bind(&body); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid JSON body")
	}
	from, err := time.Parse("2006-01-02", body.From)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "from must be YYYY-MM-DD")
	}
	to, err := time.Parse("2006-01-02", body.To)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "to must be YYYY-MM-DD")
	}
	days, err := h.st.Rebuild(c.Request().Context(), from, to)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"recomputed_employee_days": days})
}

func (h *Handler) cleanup(c echo.Context) error {
	var body struct {
		Before string `json:"before"` // RFC3339; raw snapshots strictly older are deleted
	}
	if err := c.Bind(&body); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid JSON body")
	}
	before, err := time.Parse(time.RFC3339, body.Before)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "before must be RFC3339")
	}
	deleted, err := h.st.Cleanup(c.Request().Context(), before)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"deleted_raw_snapshots": deleted})
}

// ---------------------------------------------------------------------------
// Error mapping
// ---------------------------------------------------------------------------

func errorHandler(err error, c echo.Context) {
	if c.Response().Committed {
		return
	}
	status := http.StatusInternalServerError
	msg := "internal error"
	switch {
	case errors.As(err, &store.ErrValidation{}):
		status, msg = http.StatusBadRequest, err.Error()
	case errors.As(err, &store.ErrConflict{}):
		status, msg = http.StatusConflict, err.Error()
	case errors.As(err, &store.ErrBeforeCutoff{}):
		status, msg = http.StatusUnprocessableEntity, err.Error()
	default:
		var he *echo.HTTPError
		if errors.As(err, &he) {
			status = he.Code
			msg = fmt.Sprintf("%v", he.Message)
		} else {
			c.Logger().Error(err)
		}
	}
	c.JSON(status, map[string]string{"error": msg})
}
