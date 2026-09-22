package auth

import (
	"errors"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

type Claims struct {
	UserID   string `json:"uid"`
	Username string `json:"usr"`
	Role     string `json:"role"`
	jwt.RegisteredClaims
}

// IssueToken 签发 HS256 JWT。
func IssueToken(secret []byte, ttl time.Duration, userID, username, role string) (string, time.Time, error) {
	exp := time.Now().UTC().Add(ttl)
	c := Claims{
		UserID:   userID,
		Username: username,
		Role:     role,
		RegisteredClaims: jwt.RegisteredClaims{
			ExpiresAt: jwt.NewNumericDate(exp),
			IssuedAt:  jwt.NewNumericDate(time.Now().UTC()),
			Issuer:    "activityguard",
			Subject:   userID,
		},
	}
	tok := jwt.NewWithClaims(jwt.SigningMethodHS256, c)
	s, err := tok.SignedString(secret)
	return s, exp, err
}

// ParseToken 校验并解析 JWT。
func ParseToken(secret []byte, token string) (*Claims, error) {
	c := &Claims{}
	tok, err := jwt.ParseWithClaims(token, c, func(t *jwt.Token) (any, error) {
		if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, errors.New("unexpected signing method")
		}
		return secret, nil
	}, jwt.WithIssuer("activityguard"))
	if err != nil {
		return nil, err
	}
	if !tok.Valid {
		return nil, errors.New("invalid token")
	}
	return c, nil
}
