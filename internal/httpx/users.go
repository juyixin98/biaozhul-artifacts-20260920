package httpx

import (
	"net/http"

	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"

	"synapticgo/internal/auth"
)

type userHandler struct {
	db *sqlx.DB
}

type createUserReq struct {
	Username string `json:"username"`
}

type createUserResp struct {
	ID       int64  `json:"id"`
	Username string `json:"username"`
	APIKey   string `json:"api_key"` // shown exactly once
}

func (h *userHandler) create(c echo.Context) error {
	var req createUserReq
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid JSON body")
	}
	if req.Username == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "username required")
	}
	id, key, err := auth.CreateUser(h.db, req.Username)
	if err != nil {
		if isUniqueViolation(err) {
			return echo.NewHTTPError(http.StatusConflict, "username already taken")
		}
		return fail(c, err)
	}
	return c.JSON(http.StatusCreated, createUserResp{ID: id, Username: req.Username, APIKey: key})
}
