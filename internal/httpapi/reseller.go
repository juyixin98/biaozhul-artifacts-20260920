package httpapi

import (
	"net/http"
	"strconv"

	"domainengine/internal/apierror"
	"domainengine/internal/domains"
	"domainengine/internal/models"

	"github.com/labstack/echo/v4"
)

type createCustomerReq struct {
	Name string `json:"name"`
}

func (s *Server) createCustomer(c echo.Context) error {
	p := principalFrom(c)
	var req createCustomerReq
	if err := c.Bind(&req); err != nil || req.Name == "" || p.ResellerID == nil {
		return writeError(c, apierror.ErrBadRequest)
	}
	cust, token, err := s.accounts.CreateCustomer(c.Request().Context(), *p.ResellerID, req.Name)
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(http.StatusCreated, map[string]any{
		"customer": cust,
		// Plaintext customer token shown exactly once.
		"token": token,
	})
}

func (s *Server) listCustomers(c echo.Context) error {
	p := principalFrom(c)
	if p.ResellerID == nil {
		return writeError(c, apierror.ErrForbidden)
	}
	cs, err := s.accounts.ListCustomers(c.Request().Context(), *p.ResellerID)
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(200, map[string]any{"customers": cs})
}

type registerReq struct {
	Name           string  `json:"name"`
	CustomerID     int64   `json:"customer_id"`
	Years          int     `json:"years"`
	IdempotencyKey *string `json:"idempotency_key"`
}

// registerDomain bills the calling reseller and creates the name for one of
// their customers. The plaintext 16-char auth code is returned exactly once.
func (s *Server) registerDomain(c echo.Context) error {
	p := principalFrom(c)
	var req registerReq
	if err := c.Bind(&req); err != nil {
		return writeError(c, apierror.ErrBadRequest)
	}
	if req.Years == 0 {
		req.Years = 1
	}
	if p.ResellerID == nil {
		return writeError(c, apierror.ErrForbidden)
	}
	if !s.customerBelongs(c, req.CustomerID, *p.ResellerID) {
		return writeError(c, apierror.ErrForbidden)
	}
	res, err := s.domains.Register(c.Request().Context(), s.clock, domains.RegisterRequest{
		Name:           req.Name,
		CustomerID:     req.CustomerID,
		ResellerID:     *p.ResellerID,
		Years:          req.Years,
		IdempotencyKey: req.IdempotencyKey,
	})
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(http.StatusCreated, registerResponse(res))
}

func registerResponse(res *domains.Result) map[string]any {
	return map[string]any{
		"domain":    res.Domain,
		"auth_code": res.AuthCode, // one-time reveal
		"charge":    res.ChargeTx,
	}
}

func (s *Server) listDomains(c echo.Context) error {
	p := principalFrom(c)
	limit, _ := strconv.Atoi(c.QueryParam("limit"))
	sc := scopeFor(p)
	ds, err := s.domains.List(c.Request().Context(), sc, limit)
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(200, map[string]any{"domains": ds})
}

// getDomain enforces visibility before returning a single name.
func (s *Server) getDomain(c echo.Context) error {
	p := principalFrom(c)
	d, err := s.domains.Get(c.Request().Context(), c.Param("name"))
	if err != nil {
		return writeError(c, err)
	}
	if !visibleTo(p, d) {
		return writeError(c, apierror.ErrForbidden)
	}
	return c.JSON(200, map[string]any{"domain": d})
}

type renewReq struct {
	Years          int     `json:"years"`
	IdempotencyKey *string `json:"idempotency_key"`
}

func (s *Server) renewDomain(c echo.Context) error {
	p := principalFrom(c)
	d, err := s.domains.Get(c.Request().Context(), c.Param("name"))
	if err != nil {
		return writeError(c, err)
	}
	resellerID, ok := billingReseller(p, d)
	if !ok {
		return writeError(c, apierror.ErrForbidden)
	}
	var req renewReq
	_ = c.Bind(&req) // body optional (defaults to 1 year)
	if req.Years == 0 {
		req.Years = 1
	}
	res, err := s.domains.Renew(c.Request().Context(), s.clock, domains.RenewRequest{
		Name:           d.CanonicalName,
		ResellerID:     resellerID,
		Years:          req.Years,
		IdempotencyKey: req.IdempotencyKey,
	})
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(200, map[string]any{"domain": res.Domain, "charge": res.ChargeTx})
}

type restoreReq struct {
	IdempotencyKey *string `json:"idempotency_key"`
}

func (s *Server) restoreDomain(c echo.Context) error {
	p := principalFrom(c)
	d, err := s.domains.Get(c.Request().Context(), c.Param("name"))
	if err != nil {
		return writeError(c, err)
	}
	resellerID, ok := billingReseller(p, d)
	if !ok {
		return writeError(c, apierror.ErrForbidden)
	}
	var req restoreReq
	_ = c.Bind(&req) // body optional
	res, err := s.domains.Restore(c.Request().Context(), s.clock, domains.RestoreRequest{
		Name:           d.CanonicalName,
		ResellerID:     resellerID,
		IdempotencyKey: req.IdempotencyKey,
	})
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(200, map[string]any{"domain": res.Domain, "charge": res.ChargeTx})
}

func (s *Server) rotateAuthCode(c echo.Context) error {
	p := principalFrom(c)
	if p.ResellerID == nil {
		return writeError(c, apierror.ErrForbidden)
	}
	code, d, err := s.domains.RotateAuthCode(c.Request().Context(), *p.ResellerID, c.Param("name"))
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(200, map[string]any{"domain": d, "auth_code": code})
}

func (s *Server) billingHistory(c echo.Context) error {
	p := principalFrom(c)
	if p.ResellerID == nil {
		return writeError(c, apierror.ErrForbidden)
	}
	limit, _ := strconv.Atoi(c.QueryParam("limit"))
	h, err := s.ledger.History(c.Request().Context(), *p.ResellerID, limit)
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(200, map[string]any{"transactions": h})
}

// customerBelongs verifies a customer belongs to a reseller.
func (s *Server) customerBelongs(c echo.Context, customerID, resellerID int64) bool {
	cust, err := s.accounts.GetCustomer(c.Request().Context(), customerID)
	if err != nil {
		return false
	}
	return cust.ResellerID == resellerID
}

// visibleTo enforces customer/reseller/admin visibility of a domain.
func visibleTo(p *models.Principal, d *models.Domain) bool {
	switch p.Role {
	case "admin":
		return true
	case "reseller":
		return p.ResellerID != nil && *p.ResellerID == d.ResellerID
	case "customer":
		return p.CustomerID != nil && *p.CustomerID == d.CustomerID
	}
	return false
}

// billingReseller returns the reseller that should be charged for a request by
// p against domain d. Customers act on their own domain and bill its owner
// reseller; resellers act on their own domains.
func billingReseller(p *models.Principal, d *models.Domain) (int64, bool) {
	switch p.Role {
	case "reseller":
		if p.ResellerID != nil && *p.ResellerID == d.ResellerID {
			return d.ResellerID, true
		}
	case "customer":
		if p.CustomerID != nil && *p.CustomerID == d.CustomerID {
			return d.ResellerID, true
		}
	}
	return 0, false
}
