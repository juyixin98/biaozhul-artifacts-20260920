// Package crypto performs all real cryptographic operations used by the
// service: Ed25519 transaction signatures, address derivation, node keys
// and snapshot-manifest signatures. No hash or signature is ever faked.
package crypto

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/example/snapshotprune/internal/types"
)

// NodeKey is the persistent Ed25519 key that signs snapshot manifests.
type NodeKey struct {
	Priv ed25519.PrivateKey
	Pub  ed25519.PublicKey
}

// PubHex returns the hex-encoded public key.
func (k *NodeKey) PubHex() string { return hex.EncodeToString(k.Pub) }

type nodeKeyJSON struct {
	PrivateKey string `json:"private_key"`
}

// LoadOrCreateNodeKey loads the key from dir/node_key.json, creating a
// fresh random key (with fsynced permissions 0600) on first start.
func LoadOrCreateNodeKey(dir string) (*NodeKey, error) {
	path := filepath.Join(dir, "node_key.json")
	if data, err := os.ReadFile(path); err == nil {
		var j nodeKeyJSON
		if err := json.Unmarshal(data, &j); err != nil {
			return nil, fmt.Errorf("parse node key: %w", err)
		}
		raw, err := hex.DecodeString(j.PrivateKey)
		if err != nil || len(raw) != ed25519.PrivateKeySize {
			return nil, errors.New("node_key.json contains a malformed key")
		}
		priv := ed25519.PrivateKey(raw)
		return &NodeKey{Priv: priv, Pub: priv.Public().(ed25519.PublicKey)}, nil
	} else if !os.IsNotExist(err) {
		return nil, err
	}

	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	j := nodeKeyJSON{PrivateKey: hex.EncodeToString(priv)}
	data, err := json.MarshalIndent(j, "", "  ")
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	tmp, err := os.CreateTemp(dir, ".node_key.*")
	if err != nil {
		return nil, err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return nil, err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return nil, err
	}
	if err := tmp.Close(); err != nil {
		return nil, err
	}
	if err := os.Chmod(tmpName, 0o600); err != nil {
		return nil, err
	}
	if err := os.Rename(tmpName, path); err != nil {
		return nil, err
	}
	if err := FsyncDir(dir); err != nil {
		return nil, err
	}
	return &NodeKey{Priv: priv, Pub: pub}, nil
}

// AddressFromPub derives the 20-byte address: last 20 bytes of SHA-256(pub).
func AddressFromPub(pub []byte) (types.Address, error) {
	if len(pub) != ed25519.PublicKeySize {
		return types.Address{}, fmt.Errorf("public key must be %d bytes, got %d",
			ed25519.PublicKeySize, len(pub))
	}
	var a types.Address
	sum := sha256.Sum256(pub)
	copy(a[:], sum[12:])
	return a, nil
}

// CheckTx verifies a transaction's signature and that pubkey matches From.
func CheckTx(tx *types.Tx) error {
	if len(tx.PubKey) != ed25519.PublicKeySize {
		return fmt.Errorf("transaction has invalid public key length %d", len(tx.PubKey))
	}
	if len(tx.Sig) != ed25519.SignatureSize {
		return fmt.Errorf("transaction has invalid signature length %d", len(tx.Sig))
	}
	addr, err := AddressFromPub(tx.PubKey)
	if err != nil {
		return err
	}
	if addr != tx.From {
		return fmt.Errorf("transaction signer address mismatch: pubkey derives %s, from is %s", addr.Hex(), tx.From.Hex())
	}
	if !ed25519.Verify(ed25519.PublicKey(tx.PubKey), tx.SigningBytes(), tx.Sig) {
		return errors.New("transaction signature verification failed")
	}
	return nil
}

// SignTx fills PubKey and Sig on tx using priv.
func SignTx(tx *types.Tx, priv ed25519.PrivateKey) error {
	if len(priv) != ed25519.PrivateKeySize {
		return errors.New("invalid private key")
	}
	pub := priv.Public().(ed25519.PublicKey)
	addr, err := AddressFromPub(pub)
	if err != nil {
		return err
	}
	tx.From = addr
	tx.PubKey = append([]byte(nil), pub...)
	tx.Sig = ed25519.Sign(priv, tx.SigningBytes())
	return nil
}

// SignManifest signs arbitrary manifest bytes with the node key.
func (k *NodeKey) SignManifest(msg []byte) []byte {
	return ed25519.Sign(k.Priv, msg)
}

// VerifyManifest checks a manifest signature against the node public key.
func VerifyManifest(pub, msg, sig []byte) error {
	if len(pub) != ed25519.PublicKeySize {
		return errors.New("invalid manifest public key length")
	}
	if len(sig) != ed25519.SignatureSize {
		return errors.New("invalid manifest signature length")
	}
	if !ed25519.Verify(ed25519.PublicKey(pub), msg, sig) {
		return errors.New("manifest signature verification failed")
	}
	return nil
}

// DemoPriv derives a deterministic *insecure* demo key from a seed string.
// Never use in production: the entropy is only the seed text.
func DemoPriv(seed string) ed25519.PrivateKey {
	seedHash := sha256.Sum256([]byte("snapshotprune-demo-key:" + seed))
	return ed25519.NewKeyFromSeed(seedHash[:])
}
