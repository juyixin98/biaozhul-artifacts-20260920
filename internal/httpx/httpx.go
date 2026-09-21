package httpx

import (
	"database/sql"
	"errors"
	"log"
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/synapticgo/synapticgo/internal/auth"
)

// Error is the single error envelope used by every JSON endpoint.
type Error struct {
	Error string `json:"error"`
}

// APIError carries an HTTP status alongside a message so handlers can return
// semantic errors through one path.
type APIError struct {
	Status  int
	Message string
}

func (e *APIError) Error() string { return e.Message }

func newAPIError(status int, msg string) *APIError {
	return &APIError{Status: status, Message: msg}
}

// Convenience constructors.
var (
	ErrBadRequest  = func(m string) error { return newAPIError(http.StatusBadRequest, m) }
	ErrUnauth      = func(m string) error { return newAPIError(http.StatusUnauthorized, m) }
	ErrForbidden   = func(m string) error { return newAPIError(http.StatusForbidden, m) }
	ErrNotFound    = func(m string) error { return newAPIError(http.StatusNotFound, m) }
	ErrConflict    = func(m string) error { return newAPIError(http.StatusConflict, m) }
	ErrUnprocess   = func(m string) error { return newAPIError(http.StatusUnprocessableEntity, m) }
	ErrInternal    = func(m string) error { return newAPIError(http.StatusInternalServerError, m) }
	ErrUnavailable = func(m string) error { return newAPIError(http.StatusServiceUnavailable, m) }
)

// Sentinel used for "no rows" translation.
var ErrNoRows = errors.New("not found")

// HandleError maps service errors onto HTTP responses.
func HandleError(c echo.Context, err error) error {
	var ae *APIError
	if errors.As(err, &ae) {
		return c.JSON(ae.Status, Error{Error: ae.Message})
	}
	if errors.Is(err, sql.ErrNoRows) {
		return c.JSON(http.StatusNotFound, Error{Error: "not found"})
	}
	var he *echo.HTTPError
	if errors.As(err, &he) {
		msg, _ := he.Message.(string)
		if msg == "" {
			msg = http.StatusText(he.Code)
		}
		return c.JSON(he.Code, Error{Error: msg})
	}
	log.Printf("internal error: %v", err)
	return c.JSON(http.StatusInternalServerError, Error{Error: "internal server error"})
}

// CurrentUser returns the authenticated user stored by the auth middleware.
func CurrentUser(c echo.Context) auth.User {
	u, _ := c.Get(auth.UserContextKey).(auth.User)
	return u
}

// JSON writes a successful JSON response.
func JSON(c echo.Context, status int, v any) error {
	return c.JSON(status, v)
}
