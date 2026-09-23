// Package cryptox contains the real cryptographic operations of the service:
// HMAC-SHA256 request signing for ingestion, constant-time signature
// verification, and HMAC-SHA256 webhook payload signing.
package cryptox

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"strconv"
	"time"
)

// SignedString builds the canonical string that both client and server sign:
// timestamp "\n" rawBody. Binding the timestamp into the signature prevents
// a captured signature from being replayed forever.
func SignedString(unixTime int64, body []byte) []byte {
	return []byte(strconv.FormatInt(unixTime, 10) + "\n" + string(body))
}

// Sign returns the lowercase hex HMAC-SHA256 of SignedString(ts, body).
func Sign(secret string, ts int64, body []byte) string {
	m := hmac.New(sha256.New, []byte(secret))
	m.Write(SignedString(ts, body))
	return hex.EncodeToString(m.Sum(nil))
}

// Verify compares a provided hex signature against the expected one in
// constant time. An unparseable signature is simply a mismatch.
func Verify(secret, gotHex string, ts int64, body []byte) bool {
	got, err := hex.DecodeString(gotHex)
	if err != nil {
		return false
	}
	m := hmac.New(sha256.New, []byte(secret))
	m.Write(SignedString(ts, body))
	return hmac.Equal(got, m.Sum(nil))
}

// VerifyFresh combines signature verification with a timestamp freshness
// window to reject replays.
func VerifyFresh(secret, sig string, ts time.Time, body []byte, now time.Time, window time.Duration) error {
	if secret == "" {
		return errors.New("server has no ingest secret configured")
	}
	diff := now.Sub(ts)
	if diff < 0 {
		diff = -diff
	}
	if diff > window {
		return fmt.Errorf("stale or future timestamp: skew %s exceeds window %s", diff, window)
	}
	if !Verify(secret, sig, ts.Unix(), body) {
		return errors.New("signature mismatch")
	}
	return nil
}

// SignPayload signs an arbitrary webhook payload: hex HMAC-SHA256 over
// timestamp "\n" payload.
func SignPayload(secret string, ts time.Time, payload []byte) string {
	return Sign(secret, ts.Unix(), payload)
}

// NewSecret returns 32 random bytes as 64 hex characters, generated with
// crypto/rand. It fails honestly if the system RNG fails.
func NewSecret() (string, error) {
	b := make([]byte, 32)
	if _, err := io.ReadFull(rand.Reader, b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}
