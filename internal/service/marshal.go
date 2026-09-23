package service

import (
	"bytes"
	"encoding/json"

	"vci/internal/crypto"
)

// canonicalStruct marshals a typed struct to JSON and re-encodes it with
// crypto.CanonicalJSON so the signed payload has the same deterministic
// byte form on issuer and verifier sides.
func canonicalStruct(v any) ([]byte, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var generic interface{}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&generic); err != nil {
		return nil, err
	}
	return crypto.CanonicalJSON(generic)
}
