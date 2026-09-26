package txn

import (
	"bufio"
	"encoding/binary"
	"errors"
	"hash/crc32"
	"io"
	"sync"
)

var bufPool = sync.Pool{
	New: func() any { return new(readerState) },
}

// maxFramePayload bounds a single WAL frame (records are small; 16 MiB is
// generous protection against a corrupt length field).
const maxFramePayload = 16 << 20

// readerState decodes frames from a reader using a small buffered reader.
type readerState struct {
	br     *bufio.Reader
	offset int64 // bytes consumed so far
}

func (r *readerState) reset(rd io.Reader) {
	if r.br == nil {
		r.br = bufio.NewReaderSize(rd, 64*1024)
	} else {
		r.br.Reset(rd)
	}
	r.offset = 0
}

// nextFrame returns the next valid frame or io.EOF. Any other error means
// the frame is torn/corrupt and the log must be truncated at r.offset.
func (r *readerState) nextFrame() (rawFrame, error) {
	var hdr [5]byte
	if _, err := io.ReadFull(r.br, hdr[:]); err != nil {
		if errors.Is(err, io.ErrUnexpectedEOF) {
			return rawFrame{}, io.EOF
		}
		return rawFrame{}, err
	}
	n := binary.BigEndian.Uint32(hdr[1:])
	if n > maxFramePayload {
		return rawFrame{}, errors.New("txn: frame payload too large")
	}
	payload := make([]byte, n)
	if _, err := io.ReadFull(r.br, payload); err != nil {
		return rawFrame{}, err // partial payload: torn tail
	}
	var crcBytes [4]byte
	if _, err := io.ReadFull(r.br, crcBytes[:]); err != nil {
		return rawFrame{}, err
	}
	h := crc32.NewIEEE()
	_, _ = h.Write(hdr[:])
	_, _ = h.Write(payload)
	if binary.BigEndian.Uint32(crcBytes[:]) != h.Sum32() {
		return rawFrame{}, errors.New("txn: frame checksum mismatch")
	}
	r.offset += 5 + int64(n) + 4
	return rawFrame{recType: hdr[0], payload: payload}, nil
}
