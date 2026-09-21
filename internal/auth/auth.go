package auth

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings"
	"time"

	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"
)

// User is the authenticated principal attached to a request context.
type User struct {
	ID        int64     `db:"id" json:"id"`
	Name      string    `db:"name" json:"name"`
	CreatedAt time.Time `db:"created_at" json:"created_at"`
}

// UserContextKey is the echo context key under which the User is stored.
const UserContextKey = "user"

// HashToken returns the SHA-256 hex digest of a raw API token. Only the digest
// is persisted, so a database leak does not reveal usable credentials.
func HashToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// GenerateToken creates 32 random bytes rendered as a hex string with an
// "sgk_" prefix (so tokens are recognisable in configuration files).
func GenerateToken() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return "sgk_" + hex.EncodeToString(b), nil
}

// Middleware authenticates requests using `Authorization: Bearer <token>`.
func Middleware(db *sqlx.DB) echo.MiddlewareFunc {
	return func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			header := c.Request().Header.Get(echo.HeaderAuthorization)
			const prefix = "Bearer "
			if !strings.HasPrefix(header, prefix) || len(header) <= len(prefix) {
				return c.JSON(http.StatusUnauthorized, map[string]string{"error": "missing bearer token"})
			}
			token := strings.TrimSpace(header[len(prefix):])
			var u User
			err := db.GetContext(c.Request().Context(), &u, `
				SELECT id, name, created_at
				FROM users
				WHERE token_hash = $1`, HashToken(token))
			if err != nil {
				return c.JSON(http.StatusUnauthorized, map[string]string{"error": "invalid token"})
			}
			c.Set(UserContextKey, u)
			return next(c)
		}
	}
}
