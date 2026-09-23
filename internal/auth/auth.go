// Package auth implements HMAC-SHA256 request authentication for sample
// ingestion. Every ingest request is signed by the source:
//
//	signing_string = timestamp_us + "\n" + nonce + "\n" + raw_request_body
//	signature      = base64( HMAC_SHA256(secret_key, signing_string) )
//
// Headers: X-Source, X-Timestamp-Usec, X-Nonce, X-Signature.
//
// Defences: timestamp skew window (replay after skew is rejected), single-use
// nonces persisted in PostgreSQL (real replay protection), constant-time MAC
// comparison.
package auth

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
)

// Sign computes the base64 HMAC-SHA256 of the canonical signing string.
func Sign(secretB64, timestampUsec, nonce string, body []byte) (string, error) {
	key, err := base64.StdEncoding.DecodeString(secretB64)
	if err != nil {
		return "", err
	}
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(timestampUsec))
	mac.Write([]byte("\n"))
	mac.Write([]byte(nonce))
	mac.Write([]byte("\n"))
	mac.Write(body)
	return base64.StdEncoding.EncodeToString(mac.Sum(nil)), nil
}

// Verify recomputes the MAC and compares it in constant time.
func Verify(secretB64, timestampUsec, nonce, signatureB64 string, body []byte) (bool, error) {
	expected, err := Sign(secretB64, timestampUsec, nonce, body)
	if err != nil {
		return false, err
	}
	return hmac.Equal([]byte(expected), []byte(signatureB64)), nil
}

// GenerateSecret returns a fresh 256-bit key as standard base64.
func GenerateSecret() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(b), nil
}

// GenerateNonce returns 128 bits of randomness, base64url without padding.
func GenerateNonce() (string, error) {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}
