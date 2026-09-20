package api

import (
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"

	"github.com/example/forensiccore/internal/config"
	"github.com/example/forensiccore/internal/service"
)

const (
	ctxUser = "auth_username"
	ctxRole = "auth_role"
)

type authHandler struct {
	svc *service.Service
}

type tokenRequest struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

type tokenResponse struct {
	Token     string `json:"token"`
	TokenType string `json:"token_type"`
	Username  string `json:"username"`
	Role      string `json:"role"`
	ExpiresAt string `json:"expires_at"`
}

func (h *authHandler) issueToken(c *gin.Context) {
	var req tokenRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid json body"})
		return
	}
	username, role, err := h.svc.Authenticate(req.Username, req.Password)
	if err != nil {
		c.JSON(http.StatusUnauthorized, gin.H{"error": err.Error()})
		return
	}
	exp := time.Now().UTC().Add(h.svc.TokenTTL())
	claims := jwt.MapClaims{
		"sub":  username,
		"role": role,
		"iat":  time.Now().UTC().Unix(),
		"exp":  exp.Unix(),
	}
	tok := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	signed, err := tok.SignedString(h.svc.JWTSecret())
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "sign token failed"})
		return
	}
	c.JSON(http.StatusOK, tokenResponse{
		Token: signed, TokenType: "Bearer", Username: username,
		Role: role, ExpiresAt: exp.Format(time.RFC3339),
	})
}

// authRequired 校验 Bearer JWT 并注入用户名/角色。
func authRequired(secret []byte) gin.HandlerFunc {
	return func(c *gin.Context) {
		header := c.GetHeader("Authorization")
		if header == "" || !strings.HasPrefix(header, "Bearer ") {
			c.AbortWithStatusJSON(http.StatusUnauthorized,
				gin.H{"error": "missing bearer token"})
			return
		}
		raw := strings.TrimPrefix(header, "Bearer ")
		tok, err := jwt.Parse(raw, func(t *jwt.Token) (any, error) {
			if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
				return nil, jwt.ErrTokenSignatureInvalid
			}
			return secret, nil
		})
		if err != nil || !tok.Valid {
			c.AbortWithStatusJSON(http.StatusUnauthorized,
				gin.H{"error": "invalid or expired token"})
			return
		}
		claims, ok := tok.Claims.(jwt.MapClaims)
		if !ok {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "bad claims"})
			return
		}
		sub, _ := claims["sub"].(string)
		role, _ := claims["role"].(string)
		if sub == "" || (role != config.RoleInvestigator && role != config.RoleAnalyst) {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "bad claims"})
			return
		}
		c.Set(ctxUser, sub)
		c.Set(ctxRole, role)
		c.Next()
	}
}

// requireRole 限制为指定角色之一。调查员可登记/移交；分析师只能查询/备注。
func requireRole(roles ...string) gin.HandlerFunc {
	allowed := map[string]bool{}
	for _, r := range roles {
		allowed[r] = true
	}
	return func(c *gin.Context) {
		role, _ := c.Get(ctxRole)
		rs, _ := role.(string)
		if !allowed[rs] {
			c.AbortWithStatusJSON(http.StatusForbidden, gin.H{
				"error": "forbidden: your role cannot perform this action",
			})
			return
		}
		c.Next()
	}
}

func currentUser(c *gin.Context) (string, string) {
	u, _ := c.Get(ctxUser)
	r, _ := c.Get(ctxRole)
	us, _ := u.(string)
	rs, _ := r.(string)
	return us, rs
}
