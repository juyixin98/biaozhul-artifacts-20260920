// Package crypto implements the HMAC-SHA256 request authentication used by
// every state-changing endpoint. Signatures are computed with the real
// crypto/hmac + crypto/sha256 primitives — never compared as plaintext.
package crypto

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"
)

const (
	// HMACHeader carries kid, a unix timestamp, a one-time random nonce and
	// the HMAC signature:
	//	X-Rollout-Signature: kid="...",ts=<unix>,nonce="<hex>",sig="<hex>"
	HMACHeader = "X-Rollout-Signature"
	// IdempotencyHeader carries the client-supplied idempotency key.
	IdempotencyHeader = "Idempotency-Key"

	// MaxSkew bounds how old a signed request may be before it is rejected as
	// a replay. Combined with one-time nonce tracking this prevents both
	// naive replay and captured-request reuse.
	MaxSkew = 5 * time.Minute
)

// ErrUnauthorized is returned for any malformed or bad signature.
var ErrUnauthorized = errors.New("unauthorized")

// NewNonce returns 16 random bytes as hex (128 bits of entropy).
func NewNonce() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(b[:]), nil
}

// CanonicalRequest is the exact byte string the client signs and the server
// re-signs: METHOD\nPATH\nTIMESTAMP\nNONCE\nSHA256(raw-body). The path must be
// the exact request-target the client used (no host, no query normalization).
func CanonicalRequest(method, path string, timestamp int64, nonce string, body []byte) []byte {
	bodyHash := sha256.Sum256(body)
	return []byte(fmt.Sprintf("%s\n%s\n%d\n%s\n%s",
		strings.ToUpper(method), path, timestamp, nonce, hex.EncodeToString(bodyHash[:])))
}

// Sign produces the header value the client sends. A fresh random nonce is
// generated per call so that two legitimate requests issued in the same second
// have distinct signatures.
func Sign(keyID string, secret []byte, method, path string, timestamp int64, body []byte) (string, error) {
	nonce, err := NewNonce()
	if err != nil {
		return "", err
	}
	return SignWithNonce(keyID, secret, method, path, timestamp, nonce, body), nil
}

// SignWithNonce signs with a caller-supplied nonce (used by the server to
// re-compute the expected signature).
func SignWithNonce(keyID string, secret []byte, method, path string, timestamp int64, nonce string, body []byte) string {
	mac := hmac.New(sha256.New, secret)
	mac.Write(CanonicalRequest(method, path, timestamp, nonce, body))
	sig := hex.EncodeToString(mac.Sum(nil))
	return fmt.Sprintf("kid=%q,ts=%d,nonce=%q,sig=%s", keyID, timestamp, nonce, sig)
}

// ParsedHeader is the decoded auth header.
type ParsedHeader struct {
	KeyID string
	TS    int64
	Nonce string
	Sig   string
}

// ParseHeader parses the header into its fields.
func ParseHeader(h string) (ParsedHeader, error) {
	var p ParsedHeader
	for _, part := range strings.Split(h, ",") {
		kv := strings.SplitN(strings.TrimSpace(part), "=", 2)
		if len(kv) != 2 {
			return ParsedHeader{}, fmt.Errorf("%w: malformed field", ErrUnauthorized)
		}
		key, val := kv[0], strings.Trim(kv[1], `"`)
		switch key {
		case "kid":
			p.KeyID = val
		case "ts":
			ts, err := strconv.ParseInt(val, 10, 64)
			if err != nil {
				return ParsedHeader{}, fmt.Errorf("%w: bad timestamp", ErrUnauthorized)
			}
			p.TS = ts
		case "nonce":
			p.Nonce = val
		case "sig":
			p.Sig = val
		}
	}
	if p.KeyID == "" || p.TS == 0 || p.Nonce == "" || p.Sig == "" {
		return ParsedHeader{}, fmt.Errorf("%w: missing field", ErrUnauthorized)
	}
	return p, nil
}

// Verify checks the header against the key, enforces the time window and does
// a constant-time MAC comparison.
func Verify(header, method, path string, now time.Time, body []byte, keyID string, secret []byte) (ParsedHeader, error) {
	p, err := ParseHeader(header)
	if err != nil {
		return ParsedHeader{}, err
	}
	if p.KeyID != keyID {
		return p, fmt.Errorf("%w: unknown key id", ErrUnauthorized)
	}
	skew := now.Sub(time.Unix(p.TS, 0))
	if skew > MaxSkew || skew < -MaxSkew {
		return p, fmt.Errorf("%w: timestamp outside ±5m window", ErrUnauthorized)
	}
	want := SignWithNonce(p.KeyID, secret, method, path, p.TS, p.Nonce, body)
	wantParsed, err := ParseHeader(want)
	if err != nil {
		return p, err
	}
	got, err := hex.DecodeString(p.Sig)
	if err != nil {
		return p, fmt.Errorf("%w: signature not hex", ErrUnauthorized)
	}
	if subtle.ConstantTimeCompare(got, mustHex(wantParsed.Sig)) != 1 {
		return p, fmt.Errorf("%w: signature mismatch", ErrUnauthorized)
	}
	return p, nil
}

func mustHex(s string) []byte {
	b, err := hex.DecodeString(s)
	if err != nil {
		panic(err)
	}
	return b
}

// NewID returns a 24-char URL-safe random identifier (144 bits of entropy).
func NewID(prefix string) (string, error) {
	var b [18]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return prefix + "_" + base64.RawURLEncoding.EncodeToString(b[:]), nil
}

// HashKey returns the SHA-256 hex digest of an idempotency key — keys are
// stored only as digests, never in plaintext.
func HashKey(key string) string {
	sum := sha256.Sum256([]byte(key))
	return hex.EncodeToString(sum[:])
}
