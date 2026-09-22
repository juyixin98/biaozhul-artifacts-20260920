// Package httpapi exposes the engine over an Echo HTTP API and enforces
// role-based access:
//
//	customer : only their own domains
//	reseller : their customers and domains belonging to them
//	admin    : resellers, customers, prices, ledger history, event log
//
// The request logger deliberately records only method/path/status/latency and
// never bodies or query strings, because transfer auth codes travel in request
// bodies and tokens in the Authorization header.
package httpapi

import (
	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"

	"domainengine/internal/accounts"
	clk "domainengine/internal/clock"
	"domainengine/internal/domains"
	"domainengine/internal/ledger"
	"domainengine/internal/models"
	"domainengine/internal/prices"
	"domainengine/internal/transfers"
)

type Server struct {
	db       *sqlx.DB
	accounts *accounts.Service
	domains  *domains.Service
	xfer     *transfers.Service
	prices   *prices.Service
	ledger   *ledger.Service
	clock    clk.Clock
}

func New(db *sqlx.DB, acc *accounts.Service, d *domains.Service, x *transfers.Service,
	p *prices.Service, l *ledger.Service, clock clk.Clock) *Server {
	return &Server{
		db: db, accounts: acc, domains: d, xfer: x, prices: p, ledger: l, clock: clock,
	}
}

// Handler attaches all routes to the given Echo instance.
func (s *Server) Handler(e *echo.Echo) {
	e.GET("/health", func(c echo.Context) error { return c.JSON(200, map[string]string{"status": "ok"}) })

	api := e.Group("/v1")
	// Every /v1 route requires a bearer token.
	api.Use(s.authMiddleware)

	// Admin routes.
	admin := api.Group("/admin", s.requireRole("admin"))
	admin.POST("/resellers", s.createReseller)
	admin.GET("/resellers", s.listResellers)
	admin.POST("/resellers/:id/topup", s.topup)
	admin.POST("/prices", s.setPrice)
	admin.GET("/prices", s.listPrices)
	admin.GET("/events", s.listEvents)

	// Reseller routes.
	res := api.Group("/reseller", s.requireRole("reseller"))
	res.POST("/customers", s.createCustomer)
	res.GET("/customers", s.listCustomers)
	res.POST("/domains", s.registerDomain)
	res.GET("/domains", s.listDomains)
	res.GET("/domains/:name", s.getDomain)
	res.POST("/domains/:name/renew", s.renewDomain)
	res.POST("/domains/:name/restore", s.restoreDomain)
	res.POST("/domains/:name/authcode", s.rotateAuthCode)
	res.POST("/transfers", s.requestTransfer)
	res.GET("/transfers", s.listTransfers)
	res.POST("/transfers/:id/approve", s.approveTransfer)
	res.POST("/transfers/:id/reject", s.rejectTransfer)
	res.POST("/transfers/:id/cancel", s.cancelTransfer)
	res.GET("/transfers/:id", s.getTransfer)
	res.GET("/billing", s.billingHistory)

	// Customer routes (scoped to the caller's own domains).
	cust := api.Group("/customer", s.requireRole("customer"))
	cust.GET("/domains", s.listDomains)
	cust.GET("/domains/:name", s.getDomain)
	cust.POST("/domains/:name/renew", s.renewDomain)
	cust.POST("/domains/:name/restore", s.restoreDomain)
	cust.GET("/transfers", s.listTransfers)
	cust.GET("/transfers/:id", s.getTransfer)
}

type ctxKey string

const principalKey ctxKey = "principal"

func principalFrom(c echo.Context) *models.Principal {
	p, _ := c.Get(string(principalKey)).(*models.Principal)
	return p
}
