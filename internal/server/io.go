package server

import (
	"errors"
	"io"
)

// readAllCapped reads r but returns an error if more than limit bytes arrive.
func readAllCapped(r io.Reader, limit int64) ([]byte, error) {
	buf := make([]byte, 0, 4096)
	lr := io.LimitReader(r, limit+1)
	for {
		if int64(len(buf)) >= limit+1 {
			return nil, errors.New("input exceeds configured size limit")
		}
		n, err := lr.Read(buf[len(buf):cap(buf)])
		buf = buf[:len(buf)+n]
		if err != nil {
			if errors.Is(err, io.EOF) {
				if int64(len(buf)) > limit {
					return nil, errors.New("input exceeds configured size limit")
				}
				return buf, nil
			}
			return nil, err
		}
		if len(buf) == cap(buf) {
			if int64(len(buf)) > limit {
				return nil, errors.New("input exceeds configured size limit")
			}
			grown := make([]byte, len(buf), cap(buf)*2)
			copy(grown, buf)
			buf = grown
		}
	}
}
