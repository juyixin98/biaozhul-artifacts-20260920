package api

import (
	"context"
	"io"
	"strconv"

	"github.com/go-chi/chi/v5"

	"github.example.com/ocimultipick/internal/digest"
)

func chiCtx(parent context.Context, rctx *chi.Context) context.Context {
	return context.WithValue(parent, chi.RouteCtxKey, rctx)
}

func itoa(n int64) string { return strconv.FormatInt(n, 10) }

func atoi(s string) int {
	n, err := strconv.Atoi(s)
	if err != nil {
		return 0
	}
	return n
}

func copyBuffer(w io.Writer, r io.Reader) (int64, error) {
	return io.Copy(w, r)
}

func mustParseDigest(s string) digest.Digest {
	d, err := digest.Parse(s)
	if err != nil {
		// Callers only pass digests that have already passed Parse.
		panic(err)
	}
	return d
}
