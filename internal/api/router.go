// Package api wires the HTTP routes and translates service errors to JSON.
package api

import (
	"encoding/csv"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/labstack/echo/v4"
	"github.com/labstack/echo/v4/middleware"

	"desklens/internal/admin"
	"desklens/internal/auth"
	"desklens/internal/ingest"
	"desklens/internal/manager"
)

type Handlers struct {
	Ingest  *ingest.Service
	Manager *manager.Service
	Admin   *admin.Service
}

func NewRouter(h Handlers, adminKey string) *echo.Echo {
	e := echo.New()
	e.HideBanner = true
	e.Use(middleware.Recover())
	e.Use(middleware.Logger())

	e.HTTPErrorHandler = errorHandler

	e.GET("/healthz", func(c echo.Context) error {
		return c.JSON(http.StatusOK, echo.Map{"status": "ok"})
	})

	api := e.Group("/api/v1")

	snap := api.Group("/snapshots", auth.IngestAuth(h.Ingest.DB))
	snap.POST("", ingestHandler(h.Ingest))

	mg := api.Group("/manager", auth.ManagerAuth(h.Manager.DB))
	mg.GET("/daily", dailyHandler(h.Manager))
	mg.GET("/weekly", weeklyHandler(h.Manager))
	mg.GET("/employees/:employee_id/details", detailHandler(h.Manager, false))
	mg.GET("/employees/:employee_id/export", detailHandler(h.Manager, true))

	ad := api.Group("/admin", auth.AdminAuth(adminKey))
	ad.POST("/policy", publishPolicyHandler(h.Admin))
	ad.POST("/classification", publishClassificationHandler(h.Admin))
	ad.POST("/rebuild", rebuildHandler(h.Admin))
	ad.POST("/retention/purge", purgeHandler(h.Admin))
	ad.GET("/retention", retentionHandler(h.Admin))

	return e
}

func errorHandler(err error, c echo.Context) {
	var ie *ingest.APIError
	var ae *admin.APIError
	var me *manager.APIError
	switch {
	case errors.As(err, &ie):
		writeErr(c, ie.HTTPStatus, ie.Code, ie.Message)
	case errors.As(err, &ae):
		writeErr(c, ae.HTTPStatus, ae.Code, ae.Message)
	case errors.As(err, &me):
		writeErr(c, me.HTTPStatus, me.Code, me.Message)
	default:
		var he *echo.HTTPError
		if errors.As(err, &he) {
			msg := fmt.Sprint(he.Message)
			_ = c.JSON(he.Code, echo.Map{"error": echo.Map{"code": "http_" + strconv.Itoa(he.Code), "message": msg}})
			return
		}
		writeErr(c, http.StatusInternalServerError, "internal_error", err.Error())
	}
}

func writeErr(c echo.Context, status int, code, msg string) {
	_ = c.JSON(status, echo.Map{"error": echo.Map{"code": code, "message": msg}})
}

func ingestHandler(svc *ingest.Service) echo.HandlerFunc {
	return func(c echo.Context) error {
		var req ingest.Request
		if err := c.Bind(&req); err != nil {
			return &ingest.APIError{HTTPStatus: http.StatusBadRequest, Code: "invalid_json", Message: err.Error()}
		}
		out, err := svc.Ingest(c.Request().Context(), auth.IngestPrincipal(c), req)
		if err != nil {
			return err
		}
		return c.JSON(http.StatusOK, out)
	}
}

func dailyHandler(svc *manager.Service) echo.HandlerFunc {
	return func(c echo.Context) error {
		p := auth.ManagerPrincipal(c)
		empID, _ := strconv.ParseInt(c.QueryParam("employee_id"), 10, 64)
		from := c.QueryParam("from")
		to := c.QueryParam("to")
		if from == "" || to == "" {
			return &manager.APIError{HTTPStatus: http.StatusBadRequest, Code: "invalid_range", Message: "from and to (YYYY-MM-DD) are required"}
		}
		rows, err := svc.DailySummary(c.Request().Context(), p, empID, from, to)
		if err != nil {
			return err
		}
		return c.JSON(http.StatusOK, echo.Map{"daily": rows})
	}
}

func weeklyHandler(svc *manager.Service) echo.HandlerFunc {
	return func(c echo.Context) error {
		p := auth.ManagerPrincipal(c)
		from := c.QueryParam("from")
		to := c.QueryParam("to")
		if from == "" || to == "" {
			return &manager.APIError{HTTPStatus: http.StatusBadRequest, Code: "invalid_range", Message: "from and to (YYYY-MM-DD) are required"}
		}
		rows, err := svc.WeeklySummary(c.Request().Context(), p, from, to)
		if err != nil {
			return err
		}
		return c.JSON(http.StatusOK, echo.Map{"weekly": rows})
	}
}

func detailHandler(svc *manager.Service, asExport bool) echo.HandlerFunc {
	return func(c echo.Context) error {
		p := auth.ManagerPrincipal(c)
		empID, err := strconv.ParseInt(c.Param("employee_id"), 10, 64)
		if err != nil {
			return &manager.APIError{HTTPStatus: http.StatusBadRequest, Code: "invalid_employee", Message: "employee_id must be an integer"}
		}
		from, err := time.Parse(time.RFC3339, c.QueryParam("from"))
		if err != nil {
			return &manager.APIError{HTTPStatus: http.StatusBadRequest, Code: "invalid_date", Message: "from must be RFC3339 UTC"}
		}
		to, err := time.Parse(time.RFC3339, c.QueryParam("to"))
		if err != nil {
			return &manager.APIError{HTTPStatus: http.StatusBadRequest, Code: "invalid_date", Message: "to must be RFC3339 UTC"}
		}
		rows, err := svc.DetailRows(c.Request().Context(), p, empID, from.UTC(), to.UTC())
		if err != nil {
			return err
		}
		if !asExport {
			return c.JSON(http.StatusOK, echo.Map{"snapshots": rows})
		}
		c.Response().Header().Set(echo.HeaderContentType, "text/csv")
		c.Response().Header().Set(echo.HeaderContentDisposition,
			fmt.Sprintf(`attachment; filename="snapshots_%d_%s_%s.csv"`, empID,
				from.UTC().Format("20060102T150405Z"), to.UTC().Format("20060102T150405Z")))
		w := csv.NewWriter(c.Response())
		_ = w.Write([]string{"bucket_time_utc", "employee_id", "workstation_id", "app_name",
			"activity_count", "category", "matched_rule_id", "policy_version", "classification_version"})
		for _, r := range rows {
			_ = w.Write([]string{
				r.BucketTime, strconv.FormatInt(r.EmployeeID, 10), r.WorkstationID, r.AppName,
				strconv.Itoa(r.ActivityCount), r.Category, r.MatchedRuleID,
				strconv.FormatInt(int64(r.PolicyVersion), 10), strconv.FormatInt(r.ClassificationVersion, 10),
			})
		}
		w.Flush()
		return w.Error()
	}
}

func publishPolicyHandler(svc *admin.Service) echo.HandlerFunc {
	return func(c echo.Context) error {
		var in admin.PolicyInput
		if err := c.Bind(&in); err != nil {
			return &admin.APIError{HTTPStatus: http.StatusBadRequest, Code: "invalid_json", Message: err.Error()}
		}
		out, err := svc.PublishPolicy(c.Request().Context(), in)
		if err != nil {
			return err
		}
		return c.JSON(http.StatusCreated, out)
	}
}

func publishClassificationHandler(svc *admin.Service) echo.HandlerFunc {
	return func(c echo.Context) error {
		var in admin.ClassificationInput
		if err := c.Bind(&in); err != nil {
			return &admin.APIError{HTTPStatus: http.StatusBadRequest, Code: "invalid_json", Message: err.Error()}
		}
		out, err := svc.PublishClassification(c.Request().Context(), in)
		if err != nil {
			return err
		}
		return c.JSON(http.StatusCreated, out)
	}
}

func rebuildHandler(svc *admin.Service) echo.HandlerFunc {
	return func(c echo.Context) error {
		var in admin.RebuildInput
		if err := c.Bind(&in); err != nil {
			return &admin.APIError{HTTPStatus: http.StatusBadRequest, Code: "invalid_json", Message: err.Error()}
		}
		out, err := svc.Rebuild(c.Request().Context(), in)
		if err != nil {
			return err
		}
		return c.JSON(http.StatusOK, out)
	}
}

func purgeHandler(svc *admin.Service) echo.HandlerFunc {
	return func(c echo.Context) error {
		var in admin.PurgeInput
		if err := c.Bind(&in); err != nil {
			return &admin.APIError{HTTPStatus: http.StatusBadRequest, Code: "invalid_json", Message: err.Error()}
		}
		out, err := svc.Purge(c.Request().Context(), in)
		if err != nil {
			return err
		}
		return c.JSON(http.StatusOK, out)
	}
}

func retentionHandler(svc *admin.Service) echo.HandlerFunc {
	return func(c echo.Context) error {
		out, err := svc.RetentionStatus(c.Request().Context())
		if err != nil {
			return err
		}
		return c.JSON(http.StatusOK, out)
	}
}
