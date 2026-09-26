package txn

import (
	"encoding/binary"
	"encoding/json"
	"hash/crc32"
	"io"
	"os"
	"path/filepath"
	"sync"
)

// walName is the on-disk write-ahead log file.
const walName = "store.wal"

// WAL record discriminators.
const (
	recClaim  byte = 'C' // processing claim (autocommit, BEFORE business work)
	recFailed byte = 'F' // claim marked failed (retryable)
	recCommit byte = 'X' // business commit: completed key + orders + ledger
)

// Frame layout: [type:1][payloadLen:4 BE][payload:N][crc32:4 BE]
// The CRC covers type, length header and payload.

// WAL is an append-only, fsync-per-record log guarded by its own mutex
// (commits may be attempted concurrently; serialization happens in DB).
type WAL struct {
	mu sync.Mutex
	f  *os.File
}

func openWAL(dir string) (*WAL, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	path := filepath.Join(dir, walName)
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	w := &WAL{f: f}
	if err := w.recoverOrTruncate(); err != nil {
		_ = f.Close()
		return nil, err
	}
	return w, nil
}

// Append writes one framed record and fsyncs it before returning.
func (w *WAL) Append(recType byte, v any) error {
	payload, err := json.Marshal(v)
	if err != nil {
		return err
	}
	w.mu.Lock()
	defer w.mu.Unlock()

	var hdr [5]byte
	hdr[0] = recType
	binary.BigEndian.PutUint32(hdr[1:], uint32(len(payload)))

	h := crc32.NewIEEE()
	_, _ = h.Write(hdr[:])
	_, _ = h.Write(payload)
	var crc [4]byte
	binary.BigEndian.PutUint32(crc[:], h.Sum32())

	if _, err := w.f.Write(hdr[:]); err != nil {
		return err
	}
	if _, err := w.f.Write(payload); err != nil {
		return err
	}
	if _, err := w.f.Write(crc[:]); err != nil {
		return err
	}
	return w.f.Sync()
}

// Close fsyncs and closes the file.
func (w *WAL) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.f == nil {
		return nil
	}
	_ = w.f.Sync()
	err := w.f.Close()
	w.f = nil
	return err
}

type rawFrame struct {
	recType byte
	payload []byte
}

// recoverOrTruncate replays valid frames and cuts off any torn tail
// (partial frame or CRC mismatch produced by a crash mid-write). It returns
// the ordered valid frames.
func (w *WAL) recoverOrTruncate() error {
	if _, err := w.f.Seek(0, io.SeekStart); err != nil {
		return err
	}
	r := bufPool.Get().(*readerState)
	r.reset(w.f)
	defer bufPool.Put(r)

	var goodOffset int64
	for {
		fr, err := r.nextFrame()
		if err == io.EOF {
			break
		}
		if err != nil {
			// Torn or corrupt tail: truncate to last good offset.
			return w.truncateTo(goodOffset)
		}
		goodOffset = r.offset
		_ = fr
	}
	_, err := w.f.Seek(0, io.SeekEnd)
	return err
}

func (w *WAL) truncateTo(offset int64) error {
	if err := w.f.Truncate(offset); err != nil {
		return err
	}
	_, err := w.f.Seek(0, io.SeekEnd)
	return err
}

// readFrames returns all valid frames, truncating a torn tail. It is used
// by Open after openWAL has repaired the file.
func readFrames(f *os.File) ([]rawFrame, error) {
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}
	r := bufPool.Get().(*readerState)
	r.reset(f)
	defer bufPool.Put(r)

	var out []rawFrame
	for {
		fr, err := r.nextFrame()
		if err == io.EOF {
			return out, nil
		}
		if err != nil {
			return nil, err
		}
		out = append(out, fr)
	}
}
