// Package mpart writes multipart/byteranges bodies (RFC 9110 §14.6) and
// precomputes their exact wire length so handlers can set Content-Length.
package mpart

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"io"

	"httprange/internal/rangespec"
)

// Part is one byte range of one representation, ready to be serialized.
type Part struct {
	ContentType string
	Range       rangespec.Resolved
	Total       int64
	Payload     []byte
}

// NewBoundary returns a 30-character ASCII boundary token that never occurs
// inside normal binary payloads with overwhelming probability.
func NewBoundary() string {
	var b [15]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failure is not recoverable for a network service.
		panic(fmt.Sprintf("mpart: cannot read random bytes: %v", err))
	}
	return "httprange-" + hex.EncodeToString(b[:])
}

// MediaType returns the multipart Content-Type including the boundary param.
func MediaType(boundary string) string {
	return "multipart/byteranges; boundary=" + boundary
}

const crlf = "\r\n"

func prologue(boundary string) []byte {
	return []byte("--" + boundary + crlf)
}

func partHeader(p Part) []byte {
	return []byte(fmt.Sprintf(
		"Content-Type: %s"+crlf+
			"Content-Range: %s"+crlf+
			crlf,
		p.ContentType, p.Range.ContentRange(p.Total),
	))
}

func epilogue(boundary string) []byte {
	return []byte("--" + boundary + "--" + crlf)
}

// Size returns the exact serialized body length for parts.
func Size(parts []Part, boundary string) int64 {
	var n int64
	for _, p := range parts {
		n += int64(len(prologue(boundary)))
		n += int64(len(partHeader(p)))
		n += int64(int64(len(p.Payload)))
		n += int64(len(crlf))
	}
	n += int64(len(epilogue(boundary)))
	return n
}

// Write streams the multipart body to w in the given part order.
func Write(w io.Writer, parts []Part, boundary string) error {
	buf := make([]byte, 0, 256)
	for _, p := range parts {
		buf = append(buf[:0], prologue(boundary)...)
		buf = append(buf, partHeader(p)...)
		if _, err := w.Write(buf); err != nil {
			return err
		}
		if _, err := w.Write(p.Payload); err != nil {
			return err
		}
		if _, err := io.WriteString(w, crlf); err != nil {
			return err
		}
	}
	if _, err := w.Write(epilogue(boundary)); err != nil {
		return err
	}
	return nil
}
