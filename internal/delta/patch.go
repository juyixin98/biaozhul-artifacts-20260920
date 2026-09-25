// Package delta generates and applies block-level delta patches between two
// artifacts.
//
// Patches bind BOTH the old (base) and new (target) content digests: a patch
// refuses to apply against any base whose digest differs from OldSum, and the
// produced result is verified against NewSum before it becomes visible.
//
// Block boundaries are content-defined: matching uses an rsync-style rolling
// weak checksum plus SHA-256 strong checksums over fixed-size blocks, so an
// insertion near the start of an artifact (which shifts every following byte)
// does not defeat block reuse.
package delta

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

// FormatVersion is the patch format version understood by this package.
const FormatVersion = "delta-patch/v1"

const (
	// OpCopy emits BlockSize bytes copied from an old-artifact block.
	OpCopy = "copy"
	// OpData emits inline literal bytes.
	OpData = "data"
)

// Op is one patch operation.
type Op struct {
	// Type is OpCopy or OpData.
	Type string `json:"type"`
	// OldIndex is the 0-based block index in the old artifact (copy only).
	OldIndex int64 `json:"oldIndex"`
	// Data holds raw literal bytes; JSON encoding uses standard base64.
	Data []byte `json:"data,omitempty"`
}

// Patch is a complete delta from OldSum to NewSum.
type Patch struct {
	Version   string `json:"version"`
	BlockSize int    `json:"blockSize"`
	OldSize   int64  `json:"oldSize"`
	NewSize   int64  `json:"newSize"`
	OldSum    string `json:"oldSum"`
	NewSum    string `json:"newSum"`
	Ops       []Op   `json:"ops"`

	// PatchSum binds the patch payload itself (everything above) and is
	// verified before application so a truncated or tampered patch is
	// rejected before any output is written. It is excluded from its own
	// computation.
	PatchSum string `json:"patchSum,omitempty"`
}

// Limits used when parsing/validating untrusted patches.
const (
	MinBlockSize = 16
	MaxBlockSize = 16 << 20 // 16 MiB
	MaxOps       = 200_000_000
	MaxDataOpLen = 64 << 20 // 64 MiB per literal op
)

// Validate performs structural and semantic validation of a decoded patch,
// WITHOUT verifying PatchSum (see VerifyPatchSum). It reconstructs NewSize from
// the ops so contradictory length fields are rejected.
func (p *Patch) Validate() error {
	if p.Version != FormatVersion {
		return fmt.Errorf("patch: unsupported version %q", p.Version)
	}
	if p.BlockSize < MinBlockSize || p.BlockSize > MaxBlockSize {
		return fmt.Errorf("patch: blockSize out of range: %d", p.BlockSize)
	}
	if p.OldSize < 0 || p.NewSize < 0 {
		return errors.New("patch: negative size")
	}
	if !validHexHash(p.OldSum) || !validHexHash(p.NewSum) {
		return errors.New("patch: invalid old/new digest")
	}
	if len(p.Ops) > MaxOps {
		return fmt.Errorf("patch: too many ops: %d", len(p.Ops))
	}

	var size int64
	oldBlocks := numBlocks(p.OldSize, int64(p.BlockSize))
	for i, op := range p.Ops {
		switch op.Type {
		case OpCopy:
			if len(op.Data) != 0 {
				return fmt.Errorf("patch: op %d: copy must not carry data", i)
			}
			if op.OldIndex < 0 || op.OldIndex >= oldBlocks {
				return fmt.Errorf("patch: op %d: oldIndex %d out of range", i, op.OldIndex)
			}
			start := op.OldIndex * int64(p.BlockSize)
			blen := int64(p.BlockSize)
			if start+blen > p.OldSize {
				blen = p.OldSize - start
			}
			size += blen
		case OpData:
			if op.OldIndex != 0 {
				return fmt.Errorf("patch: op %d: data must not set oldIndex", i)
			}
			if len(op.Data) == 0 {
				return fmt.Errorf("patch: op %d: empty data op", i)
			}
			if int64(len(op.Data)) > MaxDataOpLen {
				return fmt.Errorf("patch: op %d: data op too large", i)
			}
			size += int64(len(op.Data))
		default:
			return fmt.Errorf("patch: op %d: unknown type %q", i, op.Type)
		}
		if size < 0 || size > p.NewSize {
			return fmt.Errorf("patch: reconstructed size exceeds declared newSize at op %d", i)
		}
	}
	if size != p.NewSize {
		return fmt.Errorf("patch: reconstructed size %d != declared newSize %d", size, p.NewSize)
	}
	return nil
}

func numBlocks(size, block int64) int64 {
	if size == 0 {
		return 0
	}
	return (size-1)/block + 1
}

func validHexHash(h string) bool {
	if len(h) != 64 {
		return false
	}
	_, err := hex.DecodeString(h)
	return err == nil
}

// payloadForSum renders the canonical bytes hashed as the patch digest: the
// patch without its PatchSum field, with Go's deterministic encoding/json key
// ordering (struct field order) and no map iteration.
func (p *Patch) payloadForSum() ([]byte, error) {
	type canonical struct {
		Version   string `json:"version"`
		BlockSize int    `json:"blockSize"`
		OldSize   int64  `json:"oldSize"`
		NewSize   int64  `json:"newSize"`
		OldSum    string `json:"oldSum"`
		NewSum    string `json:"newSum"`
		Ops       []Op   `json:"ops"`
	}
	c := canonical{p.Version, p.BlockSize, p.OldSize, p.NewSize, p.OldSum, p.NewSum, p.Ops}
	if c.Ops == nil {
		c.Ops = []Op{}
	}
	return json.Marshal(c)
}

// ComputePatchSum returns the hex SHA-256 over the canonical patch payload.
func (p *Patch) ComputePatchSum() (string, error) {
	b, err := p.payloadForSum()
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), nil
}

// BindPatchSum computes and stores the patch digest. A generated patch is
// always stored bound.
func (p *Patch) BindPatchSum() error {
	s, err := p.ComputePatchSum()
	if err != nil {
		return err
	}
	p.PatchSum = s
	return nil
}

// VerifyPatchSum recomputes the digest and compares it to PatchSum.
func (p *Patch) VerifyPatchSum() error {
	want, err := p.ComputePatchSum()
	if err != nil {
		return err
	}
	if p.PatchSum == "" {
		return errors.New("patch: missing patchSum")
	}
	if want != p.PatchSum {
		return fmt.Errorf("patch: patchSum mismatch (patch corrupt or tampered): got %s want %s", p.PatchSum, want)
	}
	return nil
}

// Marshal JSON-encodes a bound patch.
func (p *Patch) Marshal() ([]byte, error) {
	return json.Marshal(p)
}

// UnmarshalPatch decodes a patch strictly (unknown fields rejected), without
// validating it; call Validate + VerifyPatchSum before use.
func UnmarshalPatch(b []byte) (*Patch, error) {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	var p Patch
	if err := dec.Decode(&p); err != nil {
		return nil, fmt.Errorf("patch: decode: %w", err)
	}
	if dec.More() {
		return nil, errors.New("patch: trailing data after patch document")
	}
	return &p, nil
}

// DigestBytes returns the SHA-256 hex digest of a byte slice.
func DigestBytes(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

// DigestReader streams and digests r, returning the hex SHA-256.
func DigestReader(r io.Reader) (string, error) {
	h := sha256.New()
	if _, err := io.Copy(h, r); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// encodeData is the JSON-safe representation of literal bytes. json.Marshal on
// []byte already emits base64; this helper documents the wire format and
// supports tests.
func encodeData(b []byte) string { return base64.StdEncoding.EncodeToString(b) }
