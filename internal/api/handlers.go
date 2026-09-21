package api

import (
	"context"
	"encoding/csv"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/labstack/echo/v4"

	"desklens/internal/aggregate"
	"desklens/internal/auth"
	"desklens/internal/ingest"
	"desklens/internal/repo"
)

type Handlers struct {
	repo *repo.Repo
	ing  *ingest.Service
	agg  *aggregate.Service
}

func NewHandlers(r *repo.Repo, ing *ingest.Service, agg *aggregate.Service) *Handlers {
	return &Handlers{repo: r, ing: ing, agg: agg}
}

// Register wires routes. Snapshot ingestion is open (agents hold no user
// token); every other route requires an api token, and admin routes require
// the admin role.
func (h *Handlers) Register(e *echo.Echo) {
	e.GET("/healthz", func(c echo.Context) error { return c.NoContent(http.StatusOK) })

	v1 := e.Group("/api/v1")
	v1.POST("/snapshots", h.PostSnapshots)

	authed := v1.Group("", auth.Middleware(h.repo))
	authed.GET("/summaries/daily", h.ListDaily)
	authed.GET("/summaries/weekly", h.ListWeekly)
	authed.GET("/activity", h.ListActivity)
	authed.GET("/export/activity.csv", h.ExportActivity)

	admin := authed.Group("", auth.RequireAdmin)
	admin.POST("/rebuild", h.Rebuild)
	admin.POST("/admin/retention/purge", h.Purge)
	admin.GET("/admin/retention", h.RetentionStatus)
	admin.POST("/admin/policy", h.PublishPolicy)
	admin.POST("/admin/classification", h.PublishClassification)
}

type snapshotsRequest struct {
	Snapshots []repo.SnapshotIn `json:"snapshots"`
}

func (h *Handlers) PostSnapshots(c echo.Context) error {
	var req snapshotsRequest
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid JSON: "+err.Error())
	}
	out, err := h.ing.Ingest(c.Request().Context(), req.Snapshots)
	if err != nil {
		var be *ingest.BatchError
		if errors.As(err, &be) {
			return c.JSON(be.Status, map[string]any{
				"error":  be.Code,
				"items":  be.Items,
				"detail": be.Error(),
			})
		}
		return err
	}
	return c.JSON(http.StatusOK, out)
}

// scope returns the department filter implied by the caller's role. Managers
// are confined to their own department on every read path (list, detail and
// export share this one function, so isolation cannot diverge).
func (h *Handlers) scope(c echo.Context) (*int64, error) {
	p := auth.From(c)
	if p.Role == "admin" {
		if v := c.QueryParam("department_id"); v != "" {
			id, err := strconv.ParseInt(v, 10, 64)
			if err != nil {
				return nil, echo.NewHTTPError(http.StatusBadRequest, "bad department_id")
			}
			return &id, nil
		}
		return nil, nil
	}
	if p.DepartmentID == nil {
		return nil, echo.NewHTTPError(http.StatusForbidden, "manager token without department")
	}
	// A manager may not widen scope by passing another department id.
	if v := c.QueryParam("department_id"); v != "" {
		id, err := strconv.ParseInt(v, 10, 64)
		if err == nil && id != *p.DepartmentID {
			return nil, echo.NewHTTPError(http.StatusForbidden, "cross-department access denied")
		}
	}
	return p.DepartmentID, nil
}

func parseDateParam(c echo.Context, name string) (*time.Time, error) {
	v := c.QueryParam(name)
	if v == "" {
		return nil, nil
	}
	t, err := time.Parse("2006-01-02", v)
	if err != nil {
		return nil, echo.NewHTTPError(http.StatusBadRequest, name+" must be YYYY-MM-DD")
	}
	utc := t.UTC()
	return &utc, nil
}

func employeeFilter(c echo.Context) (*int64, error) {
	if v := c.QueryParam("employee_id"); v != "" {
		id, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			return nil, echo.NewHTTPError(http.StatusBadRequest, "bad employee_id")
		}
		return &id, nil
	}
	return nil, nil
}

func (h *Handlers) ListDaily(c echo.Context) error {
	dept, err := h.scope(c)
	if err != nil {
		return err
	}
	emp, err := employeeFilter(c)
	if err != nil {
		return err
	}
	from, err := parseDateParam(c, "from")
	if err != nil {
		return err
	}
	to, err := parseDateParam(c, "to")
	if err != nil {
		return err
	}
	rows, err := h.repo.ListDaily(c.Request().Context(), repo.SummaryFilter{
		DepartmentID: dept, EmployeeID: emp, From: from, To: to,
	})
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"daily": rows})
}

func (h *Handlers) ListWeekly(c echo.Context) error {
	dept, err := h.scope(c)
	if err != nil {
		return err
	}
	from, err := parseDateParam(c, "from")
	if err != nil {
		return err
	}
	to, err := parseDateParam(c, "to")
	if err != nil {
		return err
	}
	rows, err := h.repo.ListWeekly(c.Request().Context(), repo.SummaryFilter{
		DepartmentID: dept, From: from, To: to,
	})
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"weekly": rows})
}

func (h *Handlers) ListActivity(c echo.Context) error {
	dept, err := h.scope(c)
	if err != nil {
		return err
	}
	emp, err := employeeFilter(c)
	if err != nil {
		return err
	}
	from, to, err := parseInstantRange(c)
	if err != nil {
		return err
	}
	rows, err := h.repo.ListRaw(c.Request().Context(), repo.RawFilter{
		DepartmentID: dept, EmployeeID: emp, From: from, To: to,
	})
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"activity": rows})
}

// ExportActivity streams raw rows as CSV through the exact same department
// scope as the JSON detail endpoint.
func (h *Handlers) ExportActivity(c echo.Context) error {
	dept, err := h.scope(c)
	if err != nil {
		return err
	}
	emp, err := employeeFilter(c)
	if err != nil {
		return err
	}
	from, to, err := parseInstantRange(c)
	if err != nil {
		return err
	}
	rows, err := h.repo.ListRaw(c.Request().Context(), repo.RawFilter{
		DepartmentID: dept, EmployeeID: emp, From: from, To: to, Limit: 10000,
	})
	if err != nil {
		return err
	}
	c.Response().Header().Set(echo.HeaderContentType, "text/csv; charset=utf-8")
	c.Response().Header().Set(echo.HeaderContentDisposition,
		`attachment; filename="activity.csv"`)
	w := csv.NewWriter(c.Response())
	if err := w.Write([]string{
		"minute_utc", "workstation_id", "employee_id", "department_id",
		"app_name", "activity_count", "category",
		"policy_version", "classification_version", "local_date",
	}); err != nil {
		return err
	}
	for _, r := range rows {
		if err := w.Write([]string{
			r.MinuteUTC.UTC().Format(time.RFC3339),
			strconv.FormatInt(r.WorkstationID, 10),
			strconv.FormatInt(r.EmployeeID, 10),
			strconv.FormatInt(r.DepartmentID, 10),
			r.AppName,
			strconv.Itoa(r.ActivityCount),
			r.Category,
			strconv.Itoa(r.PolicyVersion),
			strconv.Itoa(r.ClassifVersion),
			r.LocalDate.Format("2006-01-02"),
		}); err != nil {
			return err
		}
	}
	w.Flush()
	return w.Error()
}

func parseInstantRange(c echo.Context) (*time.Time, *time.Time, error) {
	var from, to *time.Time
	parse := func(name string) (*time.Time, error) {
		v := c.QueryParam(name)
		if v == "" {
			return nil, nil
		}
		t, err := time.Parse(time.RFC3339, v)
		if err != nil {
			return nil, echo.NewHTTPError(http.StatusBadRequest, name+" must be RFC3339")
		}
		u := t.UTC()
		return &u, nil
	}
	var err error
	if from, err = parse("from"); err != nil {
		return nil, nil, err
	}
	if to, err = parse("to"); err != nil {
		return nil, nil, err
	}
	return from, to, nil
}

type rebuildRequest struct {
	Scope        string `json:"scope"` // "all" (default) | "day" | "week"
	EmployeeID   int64  `json:"employee_id"`
	DepartmentID int64  `json:"department_id"`
	Date         string `json:"date"` // day: YYYY-MM-DD; week: Monday YYYY-MM-DD
}

func (h *Handlers) Rebuild(c echo.Context) error {
	var req rebuildRequest
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid JSON: "+err.Error())
	}
	if req.Scope == "" {
		req.Scope = "all"
	}
	ctx := c.Request().Context()
	switch req.Scope {
	case "all":
		report, err := h.agg.RebuildAll(ctx)
		if err != nil {
			return err
		}
		return c.JSON(http.StatusOK, report)
	case "day":
		day, err := time.Parse("2006-01-02", req.Date)
		if err != nil || req.EmployeeID == 0 {
			return echo.NewHTTPError(http.StatusBadRequest, "day rebuild needs employee_id and date")
		}
		if err := h.agg.RebuildDaily(ctx, req.EmployeeID, day.UTC()); err != nil {
			return h.rebuildErr(err)
		}
		return c.JSON(http.StatusOK, map[string]string{"status": "rebuilt"})
	case "week":
		day, err := time.Parse("2006-01-02", req.Date)
		if err != nil || req.DepartmentID == 0 {
			return echo.NewHTTPError(http.StatusBadRequest, "week rebuild needs department_id and Monday date")
		}
		if err := h.agg.RebuildWeekly(ctx, req.DepartmentID, day.UTC()); err != nil {
			return h.rebuildErr(err)
		}
		return c.JSON(http.StatusOK, map[string]string{"status": "rebuilt"})
	default:
		return echo.NewHTTPError(http.StatusBadRequest, "scope must be all|day|week")
	}
}

func (h *Handlers) rebuildErr(err error) error {
	if errors.Is(err, aggregate.ErrPartialCoverage) {
		return echo.NewHTTPError(http.StatusConflict, err.Error())
	}
	return err
}

type purgeRequest struct {
	OlderThanUTC string `json:"older_than_utc"`
}

func (h *Handlers) Purge(c echo.Context) error {
	var req purgeRequest
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid JSON")
	}
	cutoff, err := time.Parse(time.RFC3339, req.OlderThanUTC)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "older_than_utc must be RFC3339")
	}
	purged, err := h.repo.PurgeRaw(c.Request().Context(), cutoff.UTC())
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{
		"older_than_utc": cutoff.UTC().Format(time.RFC3339),
		"purged_rows":    purged,
		"summaries":      "retained",
	})
}

func (h *Handlers) RetentionStatus(c echo.Context) error {
	ctx := context.Background()
	bounds, err := h.repo.RetentionBounds(ctx)
	if err != nil {
		return err
	}
	runs, err := h.repo.ListRetentionRuns(ctx)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{
		"rebuildable_raw_window": bounds,
		"retention_runs":         runs,
	})
}

type policyRequest struct {
	WindowStartMinute int      `json:"window_start_minute"`
	WindowEndMinute   int      `json:"window_end_minute"`
	ExcludedApps      []string `json:"excluded_apps"`
	ExemptDepartments []int64  `json:"exempt_departments"`
}

func (h *Handlers) PublishPolicy(c echo.Context) error {
	var req policyRequest
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid JSON")
	}
	if req.WindowStartMinute < 0 || req.WindowStartMinute > 1440 ||
		req.WindowEndMinute < 0 || req.WindowEndMinute > 1440 {
		return echo.NewHTTPError(http.StatusBadRequest, "window minutes must be in [0,1440]")
	}
	p, err := h.repo.PublishPolicy(c.Request().Context(), repo.NewPolicy{
		WindowStartMinute: req.WindowStartMinute,
		WindowEndMinute:   req.WindowEndMinute,
		ExcludedPatterns:  req.ExcludedApps,
		ExemptDepartments: req.ExemptDepartments,
	})
	if err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, map[string]any{
		"published_version": p.Version,
		"note":              "applies to new ingestion only; history retains its stamped version",
	})
}

type ruleRequest struct {
	RuleID   int64  `json:"rule_id"`
	Pattern  string `json:"pattern"`
	Category string `json:"category"`
	Priority int    `json:"priority"`
}

type classificationRequest struct {
	Rules []ruleRequest `json:"rules"`
}

func (h *Handlers) PublishClassification(c echo.Context) error {
	var req classificationRequest
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid JSON")
	}
	if len(req.Rules) == 0 {
		return echo.NewHTTPError(http.StatusBadRequest, "rules required")
	}
	rules := make([]repo.ClassificationRule, 0, len(req.Rules))
	ids := map[int64]bool{}
	for i, r := range req.Rules {
		if r.RuleID <= 0 {
			return echo.NewHTTPError(http.StatusBadRequest, fmt.Sprintf("rules[%d].rule_id required", i))
		}
		if ids[r.RuleID] {
			return echo.NewHTTPError(http.StatusBadRequest, fmt.Sprintf("duplicate rule_id %d", r.RuleID))
		}
		ids[r.RuleID] = true
		if r.Pattern == "" {
			return echo.NewHTTPError(http.StatusBadRequest, fmt.Sprintf("rules[%d].pattern required", i))
		}
		switch r.Category {
		case "productive", "non_productive", "neutral":
		default:
			return echo.NewHTTPError(http.StatusBadRequest, fmt.Sprintf("rules[%d].category invalid", i))
		}
		rules = append(rules, repo.ClassificationRule{
			RuleID: r.RuleID, Pattern: r.Pattern,
			Category: r.Category, Priority: r.Priority,
		})
	}
	v, err := h.repo.PublishClassification(c.Request().Context(),
		repo.NewClassification{Rules: rules})
	if err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, map[string]any{
		"published_version": v,
		"note":              "existing snapshots retain the version stamped at ingestion",
	})
}
