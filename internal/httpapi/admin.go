package httpapi

import (
	"net/http"
	"strconv"

	"domainengine/internal/apierror"
	"domainengine/internal/events"

	"github.com/labstack/echo/v4"
)

type createResellerReq struct {
	Name          string `json:"name"`
	StartingCents int64  `json:"starting_cents"`
}

func (s *Server) createReseller(c echo.Context) error {
	var req createResellerReq
	if err := c.Bind(&req); err != nil {
		return writeError(c, apierror.ErrBadRequest)
	}
	r, token, err := s.accounts.CreateReseller(c.Request().Context(), req.Name, req.StartingCents)
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(http.StatusCreated, map[string]any{
		"reseller": r,
		// Plaintext token shown exactly once.
		"token": token,
	})
}

func (s *Server) listResellers(c echo.Context) error {
	rs, err := s.accounts.ListResellers(c.Request().Context())
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(200, map[string]any{"resellers": rs})
}

type topupReq struct {
	AmountCents    int64   `json:"amount_cents"`
	IdempotencyKey *string `json:"idempotency_key"`
}

// topup credits a reseller's available balance. It goes through the same
// ledger and idempotency guarantees, so retries never double-credit.
func (s *Server) topup(c echo.Context) error {
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return writeError(c, apierror.ErrBadRequest)
	}
	var req topupReq
	if err := c.Bind(&req); err != nil || req.AmountCents <= 0 {
		return writeError(c, apierror.ErrBadRequest)
	}
	bt, err := s.ledger.Topup(c.Request().Context(), id, req.AmountCents, req.IdempotencyKey)
	if err != nil {
		return writeError(c, err)
	}
	r, err := s.accounts.GetReseller(c.Request().Context(), id)
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(200, map[string]any{"reseller": r, "transaction": bt})
}

type setPriceReq struct {
	TLD           string `json:"tld"`
	RegisterCents int64  `json:"register_cents"`
	RenewCents    int64  `json:"renew_cents"`
	RestoreCents  int64  `json:"restore_cents"`
	TransferCents int64  `json:"transfer_cents"`
}

func (s *Server) setPrice(c echo.Context) error {
	var req setPriceReq
	if err := c.Bind(&req); err != nil {
		return writeError(c, apierror.ErrBadRequest)
	}
	pr, err := s.prices.Set(c.Request().Context(), req.TLD,
		req.RegisterCents, req.RenewCents, req.RestoreCents, req.TransferCents)
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(http.StatusCreated, pr)
}

func (s *Server) listPrices(c echo.Context) error {
	ps, err := s.prices.List(c.Request().Context())
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(200, map[string]any{"prices": ps})
}

func (s *Server) listEvents(c echo.Context) error {
	limit, _ := strconv.Atoi(c.QueryParam("limit"))
	ev, err := events.ListRecent(c.Request().Context(), s.db, limit)
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(200, map[string]any{"events": ev})
}
