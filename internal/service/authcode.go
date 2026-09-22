package service

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/subtle"
	"fmt"
)

// Transfer authorization codes are 16-character strings. They are stored
// AES-256-GCM encrypted (auth_code_enc) and are never written to logs:
// request/response logging is metadata-only and the code only ever appears
// in the single API response that returns it to the owner.

const authCodeAlphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789" // 32 chars, no ambiguous 0/O/1/I

func generateAuthCode() (string, error) {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	for i, b := range buf {
		buf[i] = authCodeAlphabet[int(b)%len(authCodeAlphabet)]
	}
	return string(buf), nil
}

func (s *Service) encryptCode(plain string) ([]byte, error) {
	block, err := aes.NewCipher(s.authKey)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	return gcm.Seal(nonce, nonce, []byte(plain), nil), nil
}

func (s *Service) decryptCode(enc []byte) (string, error) {
	block, err := aes.NewCipher(s.authKey)
	if err != nil {
		return "", err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	if len(enc) < gcm.NonceSize() {
		return "", fmt.Errorf("ciphertext too short")
	}
	plain, err := gcm.Open(nil, enc[:gcm.NonceSize()], enc[gcm.NonceSize():], nil)
	if err != nil {
		return "", err
	}
	return string(plain), nil
}

// GenerateAuthCode creates a fresh 16-char code for a domain (owner or
// admin). Regenerating invalidates the previous code.
func (s *Service) GenerateAuthCode(ctx context.Context, user *User, rawName string) (string, error) {
	name, err := normalizeOwned(ctx, s, user, rawName)
	if err != nil {
		return "", err
	}
	code, err := generateAuthCode()
	if err != nil {
		return "", err
	}
	enc, err := s.encryptCode(code)
	if err != nil {
		return "", err
	}
	res, err := s.db.ExecContext(ctx,
		`UPDATE domains SET auth_code_enc = $1, updated_at = $2 WHERE name = $3`, enc, s.clock.Now(), name)
	if err != nil {
		return "", err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return "", ErrNotFound
	}
	return code, nil
}

// RevealAuthCode returns the current code to the owner (or admin).
func (s *Service) RevealAuthCode(ctx context.Context, user *User, rawName string) (string, error) {
	name, err := normalizeOwned(ctx, s, user, rawName)
	if err != nil {
		return "", err
	}
	var enc []byte
	err = s.db.GetContext(ctx, &enc, `SELECT auth_code_enc FROM domains WHERE name = $1`, name)
	if err != nil {
		return "", ErrNotFound
	}
	if len(enc) == 0 {
		return "", ErrValidation("no authorization code has been generated yet")
	}
	return s.decryptCode(enc)
}

// normalizeOwned normalizes the name and checks the user is the owner or an admin.
func normalizeOwned(ctx context.Context, s *Service, user *User, rawName string) (string, error) {
	name, err := normalizeName(rawName)
	if err != nil {
		return "", err
	}
	if user.Role == RoleAdmin {
		return name, nil
	}
	var ownerID string
	err = s.db.GetContext(ctx, &ownerID, `SELECT owner_id FROM domains WHERE name = $1`, name)
	if err != nil {
		return "", ErrNotFound
	}
	if ownerID != user.ID {
		return "", ErrForbidden
	}
	return name, nil
}

// verifyAuthCode compares a presented code against the stored one in
// constant time.
func (s *Service) verifyAuthCode(enc []byte, presented string) bool {
	if len(enc) == 0 {
		return false
	}
	plain, err := s.decryptCode(enc)
	if err != nil {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(plain), []byte(presented)) == 1
}
