package apply

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"hash"
	"io"
	"strconv"

	"deltaupdate/internal/delta"
)

type streamHasher struct{ h hash.Hash }

func newStreamHasher() *streamHasher { return &streamHasher{h: sha256.New()} }

func (s *streamHasher) Write(p []byte) (int, error) { return s.h.Write(p) }

func (s *streamHasher) hex() string {
	return hex.EncodeToString(s.h.Sum(nil))
}

// reconstruct writes the bytes described by p.ops to out: literal bytes from
// data ops, block bytes from the read-only old artifact for copy ops. The old
// artifact is only Read/Seek'd — never written.
func reconstruct(ctx context.Context, p *delta.Patch, old io.ReadSeeker, out io.Writer) error {
	bs := int64(p.BlockSize)
	blockBuf := make([]byte, bs)

	// old may be nil for creation-from-empty patches.
	readBlock := func(idx int64, want int) ([]byte, error) {
		if old == nil {
			return nil, errors.New("apply: patch copies from an empty base")
		}
		off := idx * bs
		if _, err := old.Seek(off, io.SeekStart); err != nil {
			return nil, err
		}
		n, err := io.ReadFull(old, blockBuf[:want])
		if err != nil {
			return nil, err
		}
		return blockBuf[:n], nil
	}

	oldBlocks := (p.OldSize-1)/bs + 1
	if p.OldSize == 0 {
		oldBlocks = 0
	}

	for i, op := range p.Ops {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		switch op.Type {
		case delta.OpData:
			if _, err := out.Write(op.Data); err != nil {
				return err
			}
		case delta.OpCopy:
			if op.OldIndex < 0 || op.OldIndex >= oldBlocks {
				return errors.New("apply: copy index out of range")
			}
			// Block size: full blocks are bs; the last block may be short.
			start := op.OldIndex * bs
			want := bs
			if start+bs > p.OldSize {
				want = p.OldSize - start
			}
			b, err := readBlock(op.OldIndex, int(want))
			if err != nil {
				return err
			}
			if int64(len(b)) != want {
				return errors.New("apply: short read from base artifact")
			}
			if _, err := out.Write(b); err != nil {
				return err
			}
		default:
			return errors.New("apply: unknown op at index " + strconv.Itoa(i))
		}
	}
	return nil
}
