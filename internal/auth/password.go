package auth

import "golang.org/x/crypto/bcrypt"

// HashPassword returns a bcrypt hash at the configured cost.
func HashPassword(plain string) (string, error) {
	b, err := bcrypt.GenerateFromPassword([]byte(plain), bcrypt.DefaultCost)
	return string(b), err
}

// CompareHash verifies a plaintext password against a stored bcrypt hash.
func CompareHash(hash, plain string) error {
	return bcrypt.CompareHashAndPassword([]byte(hash), []byte(plain))
}
