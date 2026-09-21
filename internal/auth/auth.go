// Package auth issues and authenticates per-user API keys.
//
// Keys are 32 random bytes rendered as base64url and prefixed with "sk_".
// Only the hex SHA-256 of a key is stored, so a database leak reveals no
// usable credential.
package auth

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"strings"

	"github.com/jmoiron/sqlx"
)

const keyPrefix = "sk_"

// GenerateKey returns a new opaque API key.
func GenerateKey() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return keyPrefix + base64.RawURLEncoding.EncodeToString(b), nil
}

// HashKey returns the stored form of an API key.
func HashKey(key string) string {
	sum := sha256.Sum256([]byte(key))
	return hex.EncodeToString(sum[:])
}

// CreateUser inserts a user and returns the plaintext key exactly once.
func CreateUser(db sqlx.Ext, username string) (int64, string, error) {
	key, err := GenerateKey()
	if err != nil {
		return 0, "", err
	}
	var id int64
	err = db.QueryRowx(
		`INSERT INTO users(username, key_hash) VALUES ($1, $2) RETURNING id`,
		username, HashKey(key),
	).Scan(&id)
	return id, key, err
}

var ErrInvalidKey = errors.New("invalid API key")

// Authenticate resolves a Bearer token to a user id.
func Authenticate(db sqlx.Queryer, token string) (int64, error) {
	token = strings.TrimSpace(token)
	if !strings.HasPrefix(token, keyPrefix) {
		return 0, ErrInvalidKey
	}
	hash := HashKey(token)
	var row struct {
		ID      int64  `db:"id"`
		KeyHash string `db:"key_hash"`
	}
	if err := sqlx.Get(db, &row, `SELECT id, key_hash FROM users WHERE key_hash = $1`, hash); err != nil {
		return 0, ErrInvalidKey
	}
	if subtle.ConstantTimeCompare([]byte(row.KeyHash), []byte(hash)) != 1 {
		return 0, ErrInvalidKey
	}
	return row.ID, nil
}
