package httpx

import (
	"net/http"
	"strings"

	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"

	"synapticgo/internal/auth"
)

const userIDKey = "uid"

// AuthMiddleware resolves "Authorization: Bearer sk_..." to a user id.
func AuthMiddleware(db *sqlx.DB) echo.MiddlewareFunc {
	return func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			h := c.Request().Header.Get(echo.HeaderAuthorization)
			token := strings.TrimPrefix(h, "Bearer ")
			if token == h { // no Bearer prefix
				token = ""
			}
			uid, err := auth.Authenticate(db, token)
			if err != nil {
				return c.JSON(http.StatusUnauthorized, errResponse{Error: "missing or invalid API key"})
			}
			c.Set(userIDKey, uid)
			return next(c)
		}
	}
}

func callerID(c echo.Context) int64 { return c.Get(userIDKey).(int64) }
