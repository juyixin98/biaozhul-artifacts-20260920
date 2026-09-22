package httpapi

import (
	"net/http"
	"strconv"

	"domainengine/internal/apierror"
	"domainengine/internal/models"
	"domainengine/internal/transfers"

	"github.com/labstack/echo/v4"
)

type transferReq struct {
	DomainName     string  `json:"domain_name"`
	AuthCode       string  `json:"auth_code"` // secret; only accepted in body, never logged
	CustomerID     int64   `json:"customer_id"`
	IdempotencyKey *string `json:"idempotency_key"`
}

// requestTransfer starts an acquisition by the calling (gaining) reseller on
// behalf of one of their customers.
func (s *Server) requestTransfer(c echo.Context) error {
	p := principalFrom(c)
	var req transferReq
	if err := c.Bind(&req); err != nil {
		return writeError(c, apierror.ErrBadRequest)
	}
	if p.ResellerID == nil || req.AuthCode == "" || req.CustomerID == 0 {
		return writeError(c, apierror.ErrBadRequest)
	}
	if !s.customerBelongs(c, req.CustomerID, *p.ResellerID) {
		return writeError(c, apierror.ErrForbidden)
	}
	t, bt, err := s.xfer.Request(c.Request().Context(), s.clock, transfers.RequestInput{
		DomainName:     req.DomainName,
		AuthCode:       req.AuthCode,
		ToResellerID:   *p.ResellerID,
		ToCustomerID:   req.CustomerID,
		IdempotencyKey: req.IdempotencyKey,
	})
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(http.StatusCreated, map[string]any{"transfer": t, "freeze": bt})
}

func (s *Server) approveTransfer(c echo.Context) error {
	t, err := s.requireLosingTransfer(c)
	if err != nil {
		return writeError(c, err)
	}
	out, err := s.xfer.Approve(c.Request().Context(), s.clock, t.ID, t.FromResellerID)
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(200, map[string]any{"transfer": out})
}

func (s *Server) rejectTransfer(c echo.Context) error {
	t, err := s.requireLosingTransfer(c)
	if err != nil {
		return writeError(c, err)
	}
	out, err := s.xfer.Reject(c.Request().Context(), s.clock, t.ID, t.FromResellerID)
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(200, map[string]any{"transfer": out})
}

func (s *Server) cancelTransfer(c echo.Context) error {
	p := principalFrom(c)
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil || p.ResellerID == nil {
		return writeError(c, apierror.ErrBadRequest)
	}
	out, err := s.xfer.Cancel(c.Request().Context(), s.clock, id, *p.ResellerID)
	if err != nil {
		return writeError(c, err)
	}
	return c.JSON(200, map[string]any{"transfer": out})
}

func (s *Server) getTransfer(c echo.Context) error {
	p := principalFrom(c)
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return writeError(c, apierror.ErrBadRequest)
	}
	t, err := s.xfer.Get(c.Request().Context(), id)
	if err != nil {
		return writeError(c, err)
	}
	if !transferVisible(p, t) {
		return writeError(c, apierror.ErrForbidden)
	}
	return c.JSON(200, map[string]any{"transfer": t})
}

func (s *Server) listTransfers(c echo.Context) error {
	p := principalFrom(c)
	limit := queryLimit(c)
	switch {
	case p.ResellerID != nil:
		ts, err := s.xfer.ListForReseller(c.Request().Context(), *p.ResellerID, limit)
		if err != nil {
			return writeError(c, err)
		}
		return c.JSON(200, map[string]any{"transfers": ts})
	case p.CustomerID != nil:
		ts, err := s.xfer.ListForCustomer(c.Request().Context(), *p.CustomerID, limit)
		if err != nil {
			return writeError(c, err)
		}
		return c.JSON(200, map[string]any{"transfers": ts})
	}
	return writeError(c, apierror.ErrForbidden)
}

// requireLosingTransfer loads :id and verifies the caller is the losing
// reseller (the party allowed to approve/reject).
func (s *Server) requireLosingTransfer(c echo.Context) (*models.Transfer, error) {
	p := principalFrom(c)
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil || p.ResellerID == nil {
		return nil, apierror.ErrBadRequest
	}
	t, err := s.xfer.Get(c.Request().Context(), id)
	if err != nil {
		return nil, err
	}
	if *p.ResellerID != t.FromResellerID {
		return nil, apierror.ErrForbidden
	}
	return t, nil
}

func queryLimit(c echo.Context) int {
	n, _ := strconv.Atoi(c.QueryParam("limit"))
	return n
}

// transferVisible: admin sees all; a reseller only transfers they are a side
// of; a customer only transfers naming them on either side.
func transferVisible(p *models.Principal, t *models.Transfer) bool {
	switch p.Role {
	case "admin":
		return true
	case "reseller":
		return p.ResellerID != nil &&
			(*p.ResellerID == t.FromResellerID || *p.ResellerID == t.ToResellerID)
	case "customer":
		return p.CustomerID != nil &&
			(*p.CustomerID == t.FromCustomerID || *p.CustomerID == t.ToCustomerID)
	}
	return false
}
