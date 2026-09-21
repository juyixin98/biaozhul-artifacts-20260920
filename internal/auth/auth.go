package auth

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"strings"

	"proofcycle/internal/models"

	"gorm.io/gorm"
)

var (
	ErrInvalidCredentials = errors.New("invalid username or password")
	ErrInvalidToken       = errors.New("invalid or expired token")
)

func newToken() (string, error) {
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(b[:]), nil
}

// Login verifies credentials, rotates the session token and persists it.
// Only one token is live per user.
func Login(gdb *gorm.DB, username, password string) (*models.User, string, error) {
	var u models.User
	err := gdb.Where("username = ?", username).First(&u).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, "", ErrInvalidCredentials
	}
	if err != nil {
		return nil, "", err
	}
	if err := CompareHash(u.PasswordHash, password); err != nil {
		return nil, "", ErrInvalidCredentials
	}
	tok, err := newToken()
	if err != nil {
		return nil, "", err
	}
	if err := gdb.Model(&u).Update("token", tok).Error; err != nil {
		return nil, "", err
	}
	u.Token = tok
	return &u, tok, nil
}

// Logout invalidates the user's current token. The column is set to NULL
// (rather than "") because a unique index allows multiple NULLs but not
// multiple empty strings.
func Logout(gdb *gorm.DB, userID uint) error {
	return gdb.Model(&models.User{}).Where("id = ?", userID).Update("token", nil).Error
}

// ResolveToken maps a bearer token to its user.
func ResolveToken(gdb *gorm.DB, token string) (*models.User, error) {
	if len(token) != 64 {
		return nil, ErrInvalidToken
	}
	var u models.User
	err := gdb.Where("token = ?", token).First(&u).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrInvalidToken
	}
	if err != nil {
		return nil, err
	}
	return &u, nil
}

// ExtractToken pulls the token from an Authorization: Bearer header.
func ExtractToken(header string) string {
	const prefix = "Bearer "
	if len(header) <= len(prefix) {
		return ""
	}
	if !strings.EqualFold(header[:len(prefix)], prefix) {
		return ""
	}
	return strings.TrimSpace(header[len(prefix):])
}

// ConstantTimeEquals guards token comparisons against timing leakage.
func ConstantTimeEquals(a, b string) bool {
	return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1
}
