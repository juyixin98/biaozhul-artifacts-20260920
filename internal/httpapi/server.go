// Package httpapi exposes the domain lifecycle engine over HTTP (Echo).
package httpapi

import (
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"
	"github.com/labstack/echo/v4/middleware"

	"domainengine/internal/clock"
	"domainengine/internal/service"
)

type Server struct {
	svc *service.Service
	db  *sqlx.DB
	clk *clock.Offset // non-nil only when clock control is enabled
}

func NewRouter(svc *service.Service, db *sqlx.DB, clk *clock.Offset) *echo.Echo {
	s := &Server{svc: svc, db: db, clk: clk}
	e := echo.New()
	e.HideBanner = true
	e.HidePort = true
	e.HTTPErrorHandler = errorHandler
	e.Use(middleware.Recover())
	// Metadata-only request logging: no request/response bodies are logged,
	// so transfer auth codes never reach the logs.
	e.Use(middleware.LoggerWithConfig(middleware.LoggerConfig{
		Format: `{"time":"${time_rfc3339}","method":"${method}","uri":"${uri}","status":${status},"latency":"${latency_human}"}` + "\n",
	}))

	e.GET("/healthz", func(c echo.Context) error { return c.JSON(http.StatusOK, map[string]string{"status": "ok"}) })

	api := e.Group("/api", s.auth)

	api.GET("/domains", s.listDomains)
	api.POST("/domains", s.register)
	api.GET("/domains/:name", s.getDomain)
	api.POST("/domains/:name/renew", s.renew)
	api.POST("/domains/:name/auth-code", s.generateAuthCode)
	api.GET("/domains/:name/auth-code", s.revealAuthCode)

	api.GET("/transfers", s.listTransfers)
	api.POST("/transfers", s.initiateTransfer)
	api.POST("/transfers/:id/approve", s.approveTransfer)
	api.POST("/transfers/:id/reject", s.rejectTransfer)
	api.POST("/transfers/:id/cancel", s.cancelTransfer)

	api.GET("/resellers/me/balance", s.myBalance)
	api.GET("/resellers/me/ledger", s.myLedger)

	api.GET("/prices", s.currentPrices)
	api.PUT("/admin/prices", s.setPrice)
	api.POST("/admin/resellers/:id/credits", s.creditReseller)
	if clk != nil {
		api.POST("/admin/clock/advance", s.advanceClock)
		api.GET("/admin/clock", s.getClock)
	}
	return e
}

func errorHandler(err error, c echo.Context) {
	if c.Response().Committed {
		return
	}
	var apiErr *service.APIError
	var httpErr *echo.HTTPError
	switch {
	case errors.As(err, &apiErr):
		_ = c.JSON(apiErr.Status, map[string]string{"error": apiErr.Code, "message": apiErr.Message})
	case errors.As(err, &httpErr):
		_ = c.JSON(httpErr.Code, map[string]any{"error": "http_error", "message": httpErr.Message})
	default:
		_ = c.JSON(http.StatusInternalServerError, map[string]string{"error": "internal", "message": "internal server error"})
	}
}

// auth resolves X-Api-Key to a user.
func (s *Server) auth(next echo.HandlerFunc) echo.HandlerFunc {
	return func(c echo.Context) error {
		key := c.Request().Header.Get("X-Api-Key")
		if key == "" {
			return service.ErrUnauthorized
		}
		var u service.User
		err := s.db.GetContext(c.Request().Context(), &u,
			`SELECT id, api_key, role, name, reseller_id FROM users WHERE api_key = $1`, key)
		if errors.Is(err, sql.ErrNoRows) {
			return service.ErrUnauthorized
		}
		if err != nil {
			return err
		}
		c.Set("user", &u)
		return next(c)
	}
}

func user(c echo.Context) *service.User { return c.Get("user").(*service.User) }

func requireRole(c echo.Context, roles ...string) error {
	u := user(c)
	for _, r := range roles {
		if u.Role == r {
			return nil
		}
	}
	return service.ErrForbidden
}

// respond handles the (status, body, err) triple returned by the service,
// replaying stored idempotent responses (json.RawMessage) verbatim.
func respond(c echo.Context, status int, body any, err error) error {
	if err != nil {
		return err
	}
	if raw, ok := body.(json.RawMessage); ok {
		return c.JSONBlob(status, raw)
	}
	return c.JSON(status, body)
}

// --- domains ---

func (s *Server) listDomains(c echo.Context) error {
	doms, err := s.svc.ListDomains(c.Request().Context(), user(c))
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"domains": doms})
}

type registerRequest struct {
	Name           string `json:"name"`
	Years          int    `json:"years"`
	IdempotencyKey string `json:"idempotency_key"`
}

func (s *Server) register(c echo.Context) error {
	if err := requireRole(c, service.RoleCustomer); err != nil {
		return err
	}
	var req registerRequest
	if err := c.Bind(&req); err != nil {
		return service.ErrValidation("invalid JSON body")
	}
	status, body, err := s.svc.Register(c.Request().Context(), user(c), req.Name, req.Years, req.IdempotencyKey)
	return respond(c, status, body, err)
}

func (s *Server) getDomain(c echo.Context) error {
	dom, err := s.svc.GetDomain(c.Request().Context(), user(c), c.Param("name"))
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, dom)
}

type renewRequest struct {
	Years          int    `json:"years"`
	IdempotencyKey string `json:"idempotency_key"`
}

func (s *Server) renew(c echo.Context) error {
	var req renewRequest
	if err := c.Bind(&req); err != nil {
		return service.ErrValidation("invalid JSON body")
	}
	status, body, err := s.svc.Renew(c.Request().Context(), user(c), c.Param("name"), req.Years, req.IdempotencyKey)
	return respond(c, status, body, err)
}

func (s *Server) generateAuthCode(c echo.Context) error {
	code, err := s.svc.GenerateAuthCode(c.Request().Context(), user(c), c.Param("name"))
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]string{"auth_code": code})
}

func (s *Server) revealAuthCode(c echo.Context) error {
	code, err := s.svc.RevealAuthCode(c.Request().Context(), user(c), c.Param("name"))
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]string{"auth_code": code})
}

// --- transfers ---

func (s *Server) listTransfers(c echo.Context) error {
	trs, err := s.svc.ListTransfers(c.Request().Context(), user(c))
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"transfers": trs})
}

type transferRequest struct {
	Domain         string `json:"domain"`
	AuthCode       string `json:"auth_code"`
	IdempotencyKey string `json:"idempotency_key"`
}

func (s *Server) initiateTransfer(c echo.Context) error {
	if err := requireRole(c, service.RoleCustomer); err != nil {
		return err
	}
	var req transferRequest
	if err := c.Bind(&req); err != nil {
		return service.ErrValidation("invalid JSON body")
	}
	status, body, err := s.svc.InitiateTransfer(c.Request().Context(), user(c), req.Domain, req.AuthCode, req.IdempotencyKey)
	return respond(c, status, body, err)
}

func (s *Server) approveTransfer(c echo.Context) error {
	status, body, err := s.svc.ApproveTransfer(c.Request().Context(), user(c), c.Param("id"))
	return respond(c, status, body, err)
}

func (s *Server) rejectTransfer(c echo.Context) error {
	status, body, err := s.svc.CancelTransfer(c.Request().Context(), user(c), c.Param("id"), "reject")
	return respond(c, status, body, err)
}

func (s *Server) cancelTransfer(c echo.Context) error {
	status, body, err := s.svc.CancelTransfer(c.Request().Context(), user(c), c.Param("id"), "cancel")
	return respond(c, status, body, err)
}

// --- reseller ---

func (s *Server) myBalance(c echo.Context) error {
	if err := requireRole(c, service.RoleReseller); err != nil {
		return err
	}
	bal, err := s.svc.ResellerBalance(c.Request().Context(), user(c).ID)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, bal)
}

func (s *Server) myLedger(c echo.Context) error {
	if err := requireRole(c, service.RoleReseller); err != nil {
		return err
	}
	entries, err := s.svc.ResellerLedger(c.Request().Context(), user(c).ID)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"entries": entries})
}

// --- prices & admin ---

func (s *Server) currentPrices(c echo.Context) error {
	prices, err := s.svc.CurrentPrices(c.Request().Context())
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{"prices": prices})
}

type priceRequest struct {
	TLD           string     `json:"tld"`
	Action        string     `json:"action"`
	AmountCents   int64      `json:"amount_cents"`
	EffectiveFrom *time.Time `json:"effective_from"`
}

func (s *Server) setPrice(c echo.Context) error {
	if err := requireRole(c, service.RoleAdmin); err != nil {
		return err
	}
	var req priceRequest
	if err := c.Bind(&req); err != nil {
		return service.ErrValidation("invalid JSON body")
	}
	if req.TLD == "" {
		return service.ErrValidation("tld is required")
	}
	if err := s.svc.SetPrice(c.Request().Context(), req.TLD, req.Action, req.AmountCents, req.EffectiveFrom); err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]string{"status": "ok"})
}

type creditRequest struct {
	AmountCents    int64  `json:"amount_cents"`
	IdempotencyKey string `json:"idempotency_key"`
}

func (s *Server) creditReseller(c echo.Context) error {
	if err := requireRole(c, service.RoleAdmin); err != nil {
		return err
	}
	var req creditRequest
	if err := c.Bind(&req); err != nil {
		return service.ErrValidation("invalid JSON body")
	}
	status, body, err := s.svc.CreditReseller(c.Request().Context(), user(c), c.Param("id"), req.AmountCents, req.IdempotencyKey)
	return respond(c, status, body, err)
}

// --- simulated clock (local demos only) ---

type advanceRequest struct {
	Seconds int64 `json:"seconds"`
}

func (s *Server) advanceClock(c echo.Context) error {
	if err := requireRole(c, service.RoleAdmin); err != nil {
		return err
	}
	var req advanceRequest
	if err := c.Bind(&req); err != nil {
		return service.ErrValidation("invalid JSON body")
	}
	if req.Seconds < 0 {
		return service.ErrValidation("seconds must be >= 0 (time only moves forward)")
	}
	s.clk.Advance(time.Duration(req.Seconds) * time.Second)
	return s.getClock(c)
}

func (s *Server) getClock(c echo.Context) error {
	if err := requireRole(c, service.RoleAdmin); err != nil {
		return err
	}
	return c.JSON(http.StatusOK, map[string]any{
		"now":          s.clk.Now(),
		"skew_seconds": int64(s.clk.Skew() / time.Second),
	})
}
