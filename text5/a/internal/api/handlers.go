package api

import (
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"

	"desklens/internal/aggregate"
	"desklens/internal/ingest"
)

type handler struct{ db *sqlx.DB }

func parseDate(s string) (time.Time, error) {
	return time.Parse("2006-01-02", s)
}

func dateRange(c echo.Context) (from, to time.Time, err error) {
	from, err = parseDate(c.QueryParam("from"))
	if err != nil {
		return from, to, errJSON(http.StatusBadRequest, "invalid 'from' date, want YYYY-MM-DD")
	}
	to, err = parseDate(c.QueryParam("to"))
	if err != nil {
		return from, to, errJSON(http.StatusBadRequest, "invalid 'to' date, want YYYY-MM-DD")
	}
	if to.Before(from) {
		return from, to, errJSON(http.StatusBadRequest, "'to' must not be before 'from'")
	}
	return from, to, nil
}

// --- ingestion -------------------------------------------------------------

type ingestRequest struct {
	Snapshots []ingest.Snapshot `json:"snapshots"`
}

func (h *handler) postSnapshots(c echo.Context) error {
	var req ingestRequest
	if err := c.Bind(&req); err != nil {
		return errJSON(http.StatusBadRequest, "invalid JSON body")
	}
	res, err := ingest.Process(c.Request().Context(), h.db, req.Snapshots)
	if err != nil {
		var ve *ingest.ValidationError
		if errors.As(err, &ve) {
			return c.JSON(http.StatusBadRequest, map[string]any{"error": "validation failed", "problems": ve.Problems})
		}
		var ce *ingest.ConflictError
		if errors.As(err, &ce) {
			return c.JSON(http.StatusConflict, map[string]any{"error": ce.Error()})
		}
		return err
	}
	return c.JSON(http.StatusOK, res)
}

// --- reports ---------------------------------------------------------------

type dailyRow struct {
	Day              string `db:"day" json:"day"`
	EmployeeID       int64  `db:"employee_id" json:"employee_id"`
	ProductiveCount  int64  `db:"productive_count" json:"productive_count"`
	UnproductiveCnt  int64  `db:"unproductive_count" json:"unproductive_count"`
	NeutralCount     int64  `db:"neutral_count" json:"neutral_count"`
	TotalCount       int64  `db:"total_count" json:"total_count"`
	Snapshots        int    `db:"snapshots" json:"snapshots"`
}

func (h *handler) getEmployeeDaily(c echo.Context) error {
	targetID, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return errJSON(http.StatusBadRequest, "invalid employee id")
	}
	from, to, err := dateRange(c)
	if err != nil {
		return err
	}
	var deptID int64
	if err := h.db.GetContext(c.Request().Context(), &deptID,
		`SELECT department_id FROM employees WHERE id = $1`, targetID); err != nil {
		return errJSON(http.StatusNotFound, "employee not found")
	}
	if !canViewEmployee(currentUser(c), targetID, deptID) {
		return errJSON(http.StatusForbidden, "outside your department")
	}
	var rows []struct {
		Day              time.Time `db:"day"`
		EmployeeID       int64     `db:"employee_id"`
		ProductiveCount  int64     `db:"productive_count"`
		UnproductiveCnt  int64     `db:"unproductive_count"`
		NeutralCount     int64     `db:"neutral_count"`
		TotalCount       int64     `db:"total_count"`
		Snapshots        int       `db:"snapshots"`
	}
	if err := h.db.SelectContext(c.Request().Context(), &rows, `
		SELECT day, employee_id, productive_count, unproductive_count, neutral_count, total_count, snapshots
		FROM daily_summaries
		WHERE employee_id = $1 AND day >= $2 AND day <= $3
		ORDER BY day`, targetID, from, to); err != nil {
		return err
	}
	out := make([]dailyRow, len(rows))
	for i, r := range rows {
		out[i] = dailyRow{
			Day: r.Day.Format("2006-01-02"), EmployeeID: r.EmployeeID,
			ProductiveCount: r.ProductiveCount, UnproductiveCnt: r.UnproductiveCnt,
			NeutralCount: r.NeutralCount, TotalCount: r.TotalCount, Snapshots: r.Snapshots,
		}
	}
	return c.JSON(http.StatusOK, map[string]any{"employee_id": targetID, "daily": out})
}

func (h *handler) getDepartmentWeekly(c echo.Context) error {
	deptID, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return errJSON(http.StatusBadRequest, "invalid department id")
	}
	from, to, err := dateRange(c)
	if err != nil {
		return err
	}
	if !canViewDepartment(currentUser(c), deptID) {
		return errJSON(http.StatusForbidden, "outside your department")
	}
	var rows []struct {
		WeekStart        time.Time `db:"week_start"`
		ProductiveCount  int64     `db:"productive_count"`
		UnproductiveCnt  int64     `db:"unproductive_count"`
		NeutralCount     int64     `db:"neutral_count"`
		TotalCount       int64     `db:"total_count"`
		Employees        int       `db:"employees"`
	}
	if err := h.db.SelectContext(c.Request().Context(), &rows, `
		SELECT week_start, productive_count, unproductive_count, neutral_count, total_count, employees
		FROM weekly_summaries
		WHERE department_id = $1 AND week_start >= $2 AND week_start <= $3
		ORDER BY week_start`, deptID, from, to); err != nil {
		return err
	}
	out := make([]map[string]any, len(rows))
	for i, r := range rows {
		out[i] = map[string]any{
			"week_start": r.WeekStart.Format("2006-01-02"),
			"productive_count": r.ProductiveCount, "unproductive_count": r.UnproductiveCnt,
			"neutral_count": r.NeutralCount, "total_count": r.TotalCount, "employees": r.Employees,
		}
	}
	return c.JSON(http.StatusOK, map[string]any{"department_id": deptID, "weekly": out})
}

// getSnapshots returns raw per-minute detail with the same department
// isolation as the reports: employees see only themselves, managers only
// their own department, admins everything.
func (h *handler) getSnapshots(c echo.Context) error {
	u := currentUser(c)
	from, to, err := dateRange(c)
	if err != nil {
		return err
	}
	empFilter := c.QueryParam("employee_id")

	query := `
		SELECT r.workstation_id, r.employee_id, r.minute_utc, r.app_name, r.activity_count,
		       r.local_date, r.category, r.policy_version, r.rules_version
		FROM raw_snapshots r
		JOIN employees e ON e.id = r.employee_id
		WHERE r.local_date >= $1 AND r.local_date <= $2`
	args := []any{from, to}
	n := 2

	switch u.Role {
	case "admin":
		// unrestricted
	case "manager":
		n++
		query += fmt.Sprintf(" AND e.department_id = $%d", n)
		args = append(args, u.DepartmentID)
	default: // employee
		n++
		query += fmt.Sprintf(" AND r.employee_id = $%d", n)
		args = append(args, u.EmployeeID)
	}
	if empFilter != "" {
		id, err := strconv.ParseInt(empFilter, 10, 64)
		if err != nil {
			return errJSON(http.StatusBadRequest, "invalid employee_id")
		}
		n++
		query += fmt.Sprintf(" AND r.employee_id = $%d", n)
		args = append(args, id)
	}
	query += " ORDER BY r.minute_utc LIMIT 1000"

	var rows []struct {
		WorkstationID string    `db:"workstation_id" json:"workstation_id"`
		EmployeeID    int64     `db:"employee_id" json:"employee_id"`
		MinuteUTC     time.Time `db:"minute_utc" json:"minute_utc"`
		AppName       string    `db:"app_name" json:"app_name"`
		ActivityCount int       `db:"activity_count" json:"activity_count"`
		LocalDate     time.Time `db:"local_date" json:"-"`
		Category      string    `db:"category" json:"category"`
		PolicyVersion int       `db:"policy_version" json:"policy_version"`
		RulesVersion  int       `db:"rules_version" json:"rules_version"`
	}
	if err := h.db.SelectContext(c.Request().Context(), &rows, query, args...); err != nil {
		return err
	}
	type out struct {
		WorkstationID string    `json:"workstation_id"`
		EmployeeID    int64     `json:"employee_id"`
		MinuteUTC     time.Time `json:"minute_utc"`
		AppName       string    `json:"app_name"`
		ActivityCount int       `json:"activity_count"`
		LocalDate     string    `json:"local_date"`
		Category      string    `json:"category"`
		PolicyVersion int       `json:"policy_version"`
		RulesVersion  int       `json:"rules_version"`
	}
	res := make([]out, len(rows))
	for i, r := range rows {
		res[i] = out{r.WorkstationID, r.EmployeeID, r.MinuteUTC, r.AppName,
			r.ActivityCount, r.LocalDate.Format("2006-01-02"), r.Category,
			r.PolicyVersion, r.RulesVersion}
	}
	return c.JSON(http.StatusOK, map[string]any{"snapshots": res})
}

// exportDepartmentCSV streams per-employee daily summaries for a department;
// managers are limited to their own department, exactly like the JSON report.
func (h *handler) exportDepartmentCSV(c echo.Context) error {
	deptID, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return errJSON(http.StatusBadRequest, "invalid department id")
	}
	from, to, err := dateRange(c)
	if err != nil {
		return err
	}
	if !canViewDepartment(currentUser(c), deptID) {
		return errJSON(http.StatusForbidden, "outside your department")
	}
	rows, err := h.db.QueryxContext(c.Request().Context(), `
		SELECT ds.employee_id, e.name, ds.day, ds.productive_count,
		       ds.unproductive_count, ds.neutral_count, ds.total_count
		FROM daily_summaries ds
		JOIN employees e ON e.id = ds.employee_id
		WHERE e.department_id = $1 AND ds.day >= $2 AND ds.day <= $3
		ORDER BY ds.day, ds.employee_id`, deptID, from, to)
	if err != nil {
		return err
	}
	defer rows.Close()

	c.Response().Header().Set(echo.HeaderContentType, "text/csv")
	c.Response().Header().Set(echo.HeaderContentDisposition,
		fmt.Sprintf("attachment; filename=department-%d-daily.csv", deptID))
	c.Response().WriteHeader(http.StatusOK)
	w := csv.NewWriter(c.Response())
	w.Write([]string{"employee_id", "employee_name", "day", "productive", "unproductive", "neutral", "total"})
	for rows.Next() {
		var (
			empID                       int64
			name                        string
			day                         time.Time
			prod, unprod, neutral, tot  int64
		)
		if err := rows.Scan(&empID, &name, &day, &prod, &unprod, &neutral, &tot); err != nil {
			return err
		}
		w.Write([]string{
			strconv.FormatInt(empID, 10), name, day.Format("2006-01-02"),
			strconv.FormatInt(prod, 10), strconv.FormatInt(unprod, 10),
			strconv.FormatInt(neutral, 10), strconv.FormatInt(tot, 10),
		})
	}
	w.Flush()
	return rows.Err()
}

// --- admin -----------------------------------------------------------------

func (h *handler) postRebuild(c echo.Context) error {
	var req struct {
		From string `json:"from"`
		To   string `json:"to"`
	}
	if err := c.Bind(&req); err != nil {
		return errJSON(http.StatusBadRequest, "invalid JSON body")
	}
	from, err := parseDate(req.From)
	if err != nil {
		return errJSON(http.StatusBadRequest, "invalid 'from' date")
	}
	to, err := parseDate(req.To)
	if err != nil {
		return errJSON(http.StatusBadRequest, "invalid 'to' date")
	}
	days, err := aggregate.Rebuild(c.Request().Context(), h.db, from, to)
	if err != nil {
		if errors.Is(err, aggregate.ErrRangePurged) {
			return errJSON(http.StatusConflict, err.Error())
		}
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"recomputed_days": days})
}

func (h *handler) postCleanup(c echo.Context) error {
	var req struct {
		Before string `json:"before"`
	}
	if err := c.Bind(&req); err != nil {
		return errJSON(http.StatusBadRequest, "invalid JSON body")
	}
	before, err := parseDate(req.Before)
	if err != nil {
		return errJSON(http.StatusBadRequest, "invalid 'before' date")
	}
	deleted, err := aggregate.Cleanup(c.Request().Context(), h.db, before)
	if err != nil {
		return err
	}
	retained, _ := aggregate.RetainedFrom(c.Request().Context(), h.db)
	resp := map[string]any{"deleted_raw_snapshots": deleted}
	if retained != nil {
		resp["rebuildable_from"] = retained.Format("2006-01-02")
	}
	return c.JSON(http.StatusOK, resp)
}

func (h *handler) postPolicy(c echo.Context) error {
	var req struct {
		WorkStart    string   `json:"work_start"` // "HH:MM" local
		WorkEnd      string   `json:"work_end"`
		ExcludedApps []string `json:"excluded_apps"`
	}
	if err := c.Bind(&req); err != nil {
		return errJSON(http.StatusBadRequest, "invalid JSON body")
	}
	start, err1 := time.Parse("15:04", req.WorkStart)
	end, err2 := time.Parse("15:04", req.WorkEnd)
	if err1 != nil || err2 != nil || !start.Before(end) {
		return errJSON(http.StatusBadRequest, "work_start/work_end must be HH:MM with start before end")
	}
	var version int
	err := h.db.GetContext(c.Request().Context(), &version, `
		INSERT INTO policies (version, work_start, work_end, excluded_apps)
		VALUES ((SELECT COALESCE(MAX(version), 0) + 1 FROM policies), $1, $2, COALESCE($3::jsonb, '[]'))
		RETURNING version`,
		req.WorkStart, req.WorkEnd, mustJSON(req.ExcludedApps))
	if err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, map[string]any{"version": version})
}

func (h *handler) postRules(c echo.Context) error {
	var req struct {
		Rules []struct {
			Pattern  string `json:"pattern"`
			Category string `json:"category"`
			Priority int    `json:"priority"`
		} `json:"rules"`
	}
	if err := c.Bind(&req); err != nil {
		return errJSON(http.StatusBadRequest, "invalid JSON body")
	}
	if len(req.Rules) == 0 {
		return errJSON(http.StatusBadRequest, "rules must not be empty")
	}
	for _, r := range req.Rules {
		switch r.Category {
		case "productive", "unproductive", "neutral":
		default:
			return errJSON(http.StatusBadRequest, "category must be productive|unproductive|neutral")
		}
		if r.Pattern == "" {
			return errJSON(http.StatusBadRequest, "pattern must not be empty")
		}
	}
	ctx := c.Request().Context()
	tx, err := h.db.BeginTxx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var version int
	if err := tx.GetContext(ctx, &version,
		`SELECT COALESCE(MAX(version), 0) + 1 FROM classification_rules`); err != nil {
		return err
	}
	for _, r := range req.Rules {
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO classification_rules (version, pattern, category, priority)
			VALUES ($1, $2, $3, $4)`, version, r.Pattern, r.Category, r.Priority); err != nil {
			return err
		}
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, map[string]any{"version": version})
}

func mustJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}
