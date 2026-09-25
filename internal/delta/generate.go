package delta

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"sort"
)

// DefaultBlockSize is used when a block size is not specified.
const DefaultBlockSize = 4096

// blockSig is the old-artifact signature of one block.
type blockSig struct {
	index  int64
	size   int
	weak   uint32
	strong string // hex SHA-256 of the block bytes
}

// sigTable holds old-artifact block signatures indexed by weak checksum.
type sigTable struct {
	blockSize int
	full      map[uint32][]blockSig // full-sized blocks only
	ordered   []blockSig            // all blocks in index order
}

// Generate builds a Patch that reconstructs the content of new from old plus
// literal data. old and new are read sequentially; neither is mutated. The
// returned patch is bound (PatchSum populated).
//
// blockSize must be within [MinBlockSize, MaxBlockSize].
func Generate(old io.ReadSeeker, oldSize int64, new io.ReadSeeker, newSize int64, blockSize int) (*Patch, error) {
	if blockSize < MinBlockSize || blockSize > MaxBlockSize {
		return nil, errors.New("delta: blockSize out of range")
	}
	if oldSize < 0 || newSize < 0 {
		return nil, errors.New("delta: negative size")
	}

	table, oldDigest, err := buildSignatures(old, oldSize, blockSize)
	if err != nil {
		return nil, err
	}

	ops, newDigest, err := scanNew(new, newSize, table)
	if err != nil {
		return nil, err
	}

	p := &Patch{
		Version:   FormatVersion,
		BlockSize: blockSize,
		OldSize:   oldSize,
		NewSize:   newSize,
		OldSum:    oldDigest,
		NewSum:    newDigest,
		Ops:       ops,
	}
	if err := p.Validate(); err != nil {
		return nil, err
	}
	if err := p.BindPatchSum(); err != nil {
		return nil, err
	}
	return p, nil
}

// buildSignatures streams the old artifact once, producing per-block weak and
// strong checksums and the overall digest.
func buildSignatures(old io.Reader, oldSize int64, blockSize int) (*sigTable, string, error) {
	t := &sigTable{
		blockSize: blockSize,
		full:      make(map[uint32][]blockSig),
		ordered:   make([]blockSig, 0, numBlocks(oldSize, int64(blockSize))),
	}
	overall := sha256.New()
	buf := make([]byte, blockSize)
	var idx int64

	for {
		n, err := io.ReadFull(old, buf)
		if n > 0 {
			chunk := buf[:n]
			overall.Write(chunk)
			sum := sha256.Sum256(chunk)
			sig := blockSig{
				index:  idx,
				size:   n,
				strong: hex.EncodeToString(sum[:]),
			}
			var r roller
			for _, c := range chunk {
				r.add(c)
			}
			sig.weak = r.sum()
			t.ordered = append(t.ordered, sig)
			// Only full-sized blocks participate in rolling-window matching;
			// a short trailing block is matched separately at the new tail.
			if n == blockSize {
				t.full[sig.weak] = append(t.full[sig.weak], sig)
			}
			idx++
		}
		if err == io.EOF || err == io.ErrUnexpectedEOF {
			break
		}
		if err != nil {
			return nil, "", err
		}
	}

	for k := range t.full {
		v := t.full[k]
		sort.Slice(v, func(i, j int) bool { return v[i].index < v[j].index })
	}
	return t, hex.EncodeToString(overall.Sum(nil)), nil
}

// scanNew scans the new artifact byte-by-byte with a rolling window, emitting
// copy ops for blocks found in the signature table and data ops for everything
// else. Content-defined (rolling) matching is what makes this robust to
// insertions that shift all following block boundaries.
func scanNew(in io.Reader, newSize int64, t *sigTable) ([]Op, string, error) {
	const readChunk = 4096

	bs := t.blockSize
	ops := make([]Op, 0)
	overall := sha256.New()

	var ring []byte // current window contents, cap == bs
	var r roller    // weak checksum over ring

	var lit bytes.Buffer // literal bytes pending emission

	// emitData flushes pending literal bytes as a single data op.
	flushLit := func() error {
		if lit.Len() == 0 {
			return nil
		}
		b := lit.Bytes()
		if int64(len(b)) > MaxDataOpLen {
			return errors.New("delta: literal run exceeds max op size")
		}
		ops = append(ops, Op{Type: OpData, Data: append([]byte(nil), b...)})
		lit.Reset()
		return nil
	}

	// acceptMatch records a copy for sig, first flushing literal bytes that
	// precede the match. Blocks may be referenced in any order, including
	// repeated references to the same block (e.g. an artifact that duplicates
	// content): reconstruction seeks to each block by offset rather than
	// reading the base serially.
	acceptMatch := func(sig blockSig) error {
		if err := flushLit(); err != nil {
			return err
		}
		ops = append(ops, Op{Type: OpCopy, OldIndex: sig.index})
		return nil
	}

	matchStrong := func() (blockSig, bool) {
		cands := t.full[r.sum()]
		for _, c := range cands {
			s := sha256.Sum256(ring)
			if c.strong == hex.EncodeToString(s[:]) {
				return c, true
			}
		}
		return blockSig{}, false
	}

	rbuf := make([]byte, readChunk)
	for {
		n, err := in.Read(rbuf)
		for i := 0; i < n; i++ {
			c := rbuf[i]
			overall.Write([]byte{c})
			if len(ring) < bs {
				// Window still filling.
				ring = append(ring, c)
				r.add(c)
				if len(ring) == bs {
					if sig, ok := matchStrong(); ok {
						if e := acceptMatch(sig); e != nil {
							return nil, "", e
						}
						ring = ring[:0]
						r = roller{}
						continue
					}
				}
				continue
			}
			// Full window: roll one byte.
			out := ring[0]
			copy(ring, ring[1:])
			ring[bs-1] = c
			r.roll(out, c)
			// The byte leaving the window is now confirmed literal unless
			// the NEW window matches; either way it precedes the window, so
			// buffer it and let acceptMatch flush before the copy.
			lit.WriteByte(out)
			if sig, ok := matchStrong(); ok {
				if e := acceptMatch(sig); e != nil {
					return nil, "", e
				}
				ring = ring[:0]
				r = roller{}
			}
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, "", err
		}
	}

	// End of input: bytes still in the window are not yet classified. Try the
	// old artifact's short trailing block as an exact match; otherwise
	// literalize.
	if len(ring) > 0 && len(t.ordered) > 0 {
		short := t.ordered[len(t.ordered)-1]
		if short.size < bs && short.size == len(ring) {
			s := sha256.Sum256(ring)
			if short.strong == hex.EncodeToString(s[:]) {
				if e := flushLit(); e != nil {
					return nil, "", e
				}
				ops = append(ops, Op{Type: OpCopy, OldIndex: short.index})
				ring = nil
			}
		}
	}
	if len(ring) > 0 {
		lit.Write(ring)
	}
	if err := flushLit(); err != nil {
		return nil, "", err
	}

	return ops, hex.EncodeToString(overall.Sum(nil)), nil
}
