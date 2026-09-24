package attestation

import (
	"crypto/ed25519"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"
)

// TrustedKey is one Ed25519 key with its rotation window. A key is valid when
// notBefore <= t < notAfter and revokedAt is absent or later than t.
type TrustedKey struct {
	ID        string     `json:"id"`
	PublicKey string     `json:"publicKey"`
	NotBefore time.Time  `json:"notBefore"`
	NotAfter  time.Time  `json:"notAfter"`
	RevokedAt *time.Time `json:"revokedAt,omitempty"`

	pub ed25519.PublicKey
}

// TrustedBuilder lists the keys that may sign for a builder id.
type TrustedBuilder struct {
	ID   string        `json:"id"`
	Keys []*TrustedKey `json:"keys"`
}

// Policy is the static trust anchor loaded from JSON.
type Policy struct {
	// AllowedSourcePrefixes restricts where source code may come from. A
	// repository matches if it equals a prefix or, when the prefix ends with
	// "/", it is under that namespace.
	AllowedSourcePrefixes []string          `json:"allowedSourcePrefixes"`
	Builders              []*TrustedBuilder `json:"builders"`

	byBuilder map[string]*TrustedBuilder
	byKey     map[string]*trustedKeyEntry
}

type trustedKeyEntry struct {
	builder *TrustedBuilder
	key     *TrustedKey
}

// LoadPolicy reads, parses and indexes a trust-policy JSON file.
func LoadPolicy(path string) (*Policy, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading policy %s: %w", path, err)
	}
	return ParsePolicy(data)
}

// ParsePolicy parses a policy from JSON and validates every key entry.
func ParsePolicy(data []byte) (*Policy, error) {
	var p Policy
	if err := json.Unmarshal(data, &p); err != nil {
		return nil, fmt.Errorf("parsing policy: %w", err)
	}
	p.byBuilder = map[string]*TrustedBuilder{}
	p.byKey = map[string]*trustedKeyEntry{}

	if len(p.AllowedSourcePrefixes) == 0 {
		return nil, fmt.Errorf("policy must declare at least one allowedSourcePrefix")
	}
	for _, pref := range p.AllowedSourcePrefixes {
		if !strings.HasPrefix(pref, "https://") {
			return nil, fmt.Errorf("source prefix %q must start with https://", pref)
		}
	}
	if len(p.Builders) == 0 {
		return nil, fmt.Errorf("policy must declare at least one builder")
	}
	for _, b := range p.Builders {
		if b.ID == "" {
			return nil, fmt.Errorf("builder without id")
		}
		if _, dup := p.byBuilder[b.ID]; dup {
			return nil, fmt.Errorf("duplicate builder id %q", b.ID)
		}
		p.byBuilder[b.ID] = b
		if len(b.Keys) == 0 {
			return nil, fmt.Errorf("builder %q has no keys", b.ID)
		}
		for _, k := range b.Keys {
			if err := k.finish(b); err != nil {
				return nil, err
			}
			if _, dup := p.byKey[k.ID]; dup {
				return nil, fmt.Errorf("duplicate key id %q", k.ID)
			}
			p.byKey[k.ID] = &trustedKeyEntry{builder: b, key: k}
		}
	}
	return &p, nil
}

func (k *TrustedKey) finish(b *TrustedBuilder) error {
	if k.ID == "" {
		return fmt.Errorf("builder %q has a key without id", b.ID)
	}
	raw, err := hex.DecodeString(k.PublicKey)
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return fmt.Errorf("key %q: publicKey must be %d hex bytes (Ed25519)", k.ID, ed25519.PublicKeySize)
	}
	k.pub = ed25519.PublicKey(raw)
	if !k.NotBefore.Before(k.NotAfter) {
		return fmt.Errorf("key %q: notBefore must be strictly before notAfter", k.ID)
	}
	if k.RevokedAt != nil && k.RevokedAt.Before(k.NotBefore) {
		return fmt.Errorf("key %q: revokedAt must not precede notBefore", k.ID)
	}
	return nil
}

// LookupBuilder resolves a trusted builder by id.
func (p *Policy) LookupBuilder(id string) *TrustedBuilder { return p.byBuilder[id] }

// LookupKey resolves a key by id, irrespective of which builder owns it.
func (p *Policy) LookupKey(id string) (*TrustedBuilder, *TrustedKey) {
	e := p.byKey[id]
	if e == nil {
		return nil, nil
	}
	return e.builder, e.key
}

// SourceAllowed reports whether repository is covered by an allow-prefix.
func (p *Policy) SourceAllowed(repository string) bool {
	for _, pref := range p.AllowedSourcePrefixes {
		if repository == pref {
			return true
		}
		if strings.HasSuffix(pref, "/") && strings.HasPrefix(repository, pref) {
			return true
		}
	}
	return false
}

// keyStatusAt evaluates the rotation window at time t.
func keyStatusAt(k *TrustedKey, t time.Time) *VerificationError {
	if t.Before(k.NotBefore) {
		return verr(CodeKeyNotYetValid, "key %q valid from %s, now %s", k.ID, k.NotBefore.Format(time.RFC3339), t.Format(time.RFC3339))
	}
	if !t.Before(k.NotAfter) {
		return verr(CodeKeyExpired, "key %q expired at %s (rotation)", k.ID, k.NotAfter.Format(time.RFC3339))
	}
	if k.RevokedAt != nil && !t.Before(*k.RevokedAt) {
		return verr(CodeKeyRevoked, "key %q revoked at %s", k.ID, k.RevokedAt.Format(time.RFC3339))
	}
	return nil
}

// replayCache keeps seen nonces until they fall out of the freshness window.
type replayCache struct {
	mu      sync.Mutex
	expiry  map[string]time.Time
	maxSize int
}

func newReplayCache(maxSize int) *replayCache {
	if maxSize <= 0 {
		maxSize = 100_000
	}
	return &replayCache{expiry: map[string]time.Time{}, maxSize: maxSize}
}

// checkAndRemember returns an error if the nonce was already seen; otherwise it
// stores the nonce with an eviction deadline.
func (c *replayCache) checkAndRemember(nonce string, now time.Time, ttl time.Duration) *VerificationError {
	c.mu.Lock()
	defer c.mu.Unlock()

	if exp, seen := c.expiry[nonce]; seen && exp.After(now) {
		return verr(CodeReplay, "nonce %q was already used; each attestation needs a fresh nonce", nonce)
	}
	if len(c.expiry) >= c.maxSize {
		c.evictLocked(now)
		if len(c.expiry) >= c.maxSize {
			// Fails closed under memory pressure: refusing a replay cache
			// write must not silently disable replay protection.
			return verr(CodeReplay, "replay cache full; retry later")
		}
	}
	c.expiry[nonce] = now.Add(ttl)
	return nil
}

func (c *replayCache) evictLocked(now time.Time) {
	for n, exp := range c.expiry {
		if !exp.After(now) {
			delete(c.expiry, n)
		}
	}
}
