// Package auth provides password hashing, operator API keys, a small HS256 JWT
// implementation, role constants, and masking helpers.
package auth

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"golang.org/x/crypto/bcrypt"
)

const (
	RoleAdmin    = "admin"
	RoleOperator = "operator"
	RoleAuditor  = "auditor"
)

type Claims struct {
	Sub        string `json:"sub"`
	Role       string `json:"role"`
	MerchantID string `json:"mid,omitempty"`
	Email      string `json:"email,omitempty"`
	Iat        int64  `json:"iat"`
	Exp        int64  `json:"exp"`
}

func HashPassword(pw string) (string, error) {
	b, err := bcrypt.GenerateFromPassword([]byte(pw), bcrypt.DefaultCost)
	return string(b), err
}

func CheckPassword(hash, pw string) bool {
	return bcrypt.CompareHashAndPassword([]byte(hash), []byte(pw)) == nil
}

// API keys look like cs_live_<43 url-safe chars>. Only the sha256 hash is stored;
// the prefix shown back to operators is the first 12 chars.
func GenerateAPIKey() (plaintext, hash, prefix string, err error) {
	raw := make([]byte, 32)
	if _, err = rand.Read(raw); err != nil {
		return
	}
	plaintext = "cs_live_" + base64.RawURLEncoding.EncodeToString(raw)
	sum := sha256.Sum256([]byte(plaintext))
	hash = hex.EncodeToString(sum[:])
	prefix = plaintext[:12]
	return
}

func HashAPIKey(plaintext string) string {
	sum := sha256.Sum256([]byte(plaintext))
	return hex.EncodeToString(sum[:])
}

func issueToken(secret string, c Claims, ttl time.Duration) (string, error) {
	c.Iat = time.Now().Unix()
	c.Exp = time.Now().Add(ttl).Unix()
	header := map[string]string{"alg": "HS256", "typ": "JWT"}
	hb, _ := json.Marshal(header)
	cb, _ := json.Marshal(c)
	sign := func(data string) string {
		m := hmac.New(sha256.New, []byte(secret))
		m.Write([]byte(data))
		return base64.RawURLEncoding.EncodeToString(m.Sum(nil))
	}
	body := base64.RawURLEncoding.EncodeToString(hb) + "." + base64.RawURLEncoding.EncodeToString(cb)
	return body + "." + sign(body), nil
}

func IssueUserToken(secret string, c Claims) (string, error) {
	return issueToken(secret, c, 12*time.Hour)
}

func ParseToken(secret, token string) (Claims, error) {
	var c Claims
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return c, fmt.Errorf("malformed token")
	}
	sign := func(data string) string {
		m := hmac.New(sha256.New, []byte(secret))
		m.Write([]byte(data))
		return base64.RawURLEncoding.EncodeToString(m.Sum(nil))
	}
	if !hmac.Equal([]byte(sign(parts[0]+"."+parts[1])), []byte(parts[2])) {
		return c, fmt.Errorf("bad signature")
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return c, err
	}
	if err := json.Unmarshal(payload, &c); err != nil {
		return c, err
	}
	if time.Now().Unix() > c.Exp {
		return c, fmt.Errorf("token expired")
	}
	return c, nil
}

// MaskEmail keeps the first char and domain: a***@example.com
func MaskEmail(email string) string {
	at := strings.IndexByte(email, '@')
	if at <= 1 {
		return strings.Repeat("*", max0(at)) + email[at:]
	}
	return email[:1] + strings.Repeat("*", at-1) + email[at:]
}

// MaskAPIKey shows only the stored prefix.
func MaskAPIKey(prefix string) string {
	if prefix == "" {
		return ""
	}
	return prefix + "…"
}

func max0(n int) int {
	if n < 0 {
		return 0
	}
	return n
}
