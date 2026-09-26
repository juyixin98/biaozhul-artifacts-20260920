package main

import (
	"context"
	"fmt"
	"net/http"
	"time"

	"httprange/internal/artifact"
	"httprange/internal/client"
	"httprange/internal/clock"
	"httprange/internal/origin"
	"httprange/internal/report"
)

// env bundles the two in-process origins used by the suite.
type env struct {
	identity *origin.Handle // identity-only representation
	gzip     *origin.Handle // offers a gzip selected representation
	clk      *clock.Fake
	store    *artifact.Store
}

const (
	idSample = "sample.bin"
	idTiny   = "tiny.bin"
	idEmpty  = "empty.bin"
	idText   = "hello.txt"

	sampleSize = 1024
	rangeLimit = 5
)

// fixtureTime is the fixed Last-Modified instant used by every artifact.
var fixtureTime = time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)

func setupEnv(ctx context.Context) (*env, error) {
	clk := clock.NewFake(fixtureTime)
	store := seedFixtures()

	ident, err := origin.Start(ctx, origin.Config{
		Store: store, Clock: clk, MaxRanges: rangeLimit,
	})
	if err != nil {
		return nil, err
	}
	gz, err := origin.Start(ctx, origin.Config{
		Store: store, Clock: clk, EnableGzip: true, MaxRanges: rangeLimit,
	})
	if err != nil {
		_ = ident.Shutdown()
		return nil, err
	}
	return &env{identity: ident, gzip: gz, clk: &clk, store: store}, nil
}

func (e *env) shutdown() {
	_ = e.identity.Shutdown()
	_ = e.gzip.Shutdown()
}

func (e *env) c() *client.Client   { return client.New(e.identity.BaseURL) }
func (e *env) gzc() *client.Client { return client.New(e.gzip.BaseURL) }

func seedFixtures() *artifact.Store {
	pattern := func(n int, seed byte) []byte {
		b := make([]byte, n)
		for i := range b {
			b[i] = seed + byte(i%251)
		}
		return b
	}
	store := artifact.NewStore()
	store.Put(artifact.New(idSample, pattern(sampleSize, 1),
		"application/octet-stream", fixtureTime))
	store.Put(artifact.New(idTiny, pattern(26, 90),
		"application/octet-stream", fixtureTime))
	store.Put(artifact.New(idEmpty, []byte{},
		"application/octet-stream", fixtureTime))
	store.Put(artifact.New(idText, []byte("the quick brown fox jumps\n"),
		"text/plain; charset=utf-8", fixtureTime))
	return store
}

// httpDate renders t using the HTTP-date IMF-fixdate form (RFC 9110 §5.6.7):
// an RFC 1123 date whose zone is the literal "GMT", the only spelling
// http.ParseTime accepts for the numeric-offset-free layout.
func httpDate(t time.Time) string {
	return t.UTC().Format(http.TimeFormat)
}

func failf(format string, args ...any) error {
	return fmt.Errorf(format, args...)
}

// respSpec builds the structured response record for an outcome.
func respSpec(status int, headers http.Header, contentLength int64, body []byte) report.ResponseSpec {
	hdrs := map[string]string{}
	for _, k := range []string{
		"Content-Type", "Content-Range", "Content-Encoding", "ETag",
		"Accept-Ranges", "Last-Modified", "X-Range-Member-Limit",
	} {
		if v := headers.Get(k); v != "" {
			hdrs[k] = v
		}
	}
	spec := report.ResponseSpec{
		StatusCode:    status,
		Headers:       hdrs,
		ContentLength: contentLength,
		BodyLength:    len(body),
	}
	if len(body) > 0 {
		spec.BodySHA256 = client.SHA256(body)
	}
	return spec
}

// reqSpec builds the structured request record.
func reqSpec(path string, headers map[string]string) report.RequestSpec {
	return report.RequestSpec{Method: http.MethodGet, Path: path, Headers: headers}
}
