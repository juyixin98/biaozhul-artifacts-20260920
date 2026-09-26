package server

import (
	"bytes"
	"compress/flate"
	"compress/gzip"
	"strconv"
	"strings"
	"sync"

	"httprange/internal/artifact"
)

// representation is the selected representation (RFC 9110 §3.2) for one
// request: a concrete byte sequence plus its validators. Range offsets and
// Content-Range values always refer to these bytes.
type representation struct {
	data        []byte
	contentType string
	etag        string
	encoding    string // "" (identity) or "gzip"
}

func (r representation) size() int64 { return int64(len(r.data)) }

// gzipCache memoizes deterministic gzip encodings; an immutable artifact's
// encoding is itself immutable, so this is safe for concurrent use.
type gzipCache struct {
	mu    sync.Mutex
	byArt map[*artifact.Artifact][]byte
}

func newGzipCache() *gzipCache {
	return &gzipCache{byArt: make(map[*artifact.Artifact][]byte)}
}

// encodeGzip produces a deterministic gzip stream: zero header fields, fixed
// compression level. DEFLATE output of the same bytes at a fixed level is
// reproducible, hence the derived ETag is stable across processes.
func encodeGzip(src []byte) ([]byte, error) {
	var buf bytes.Buffer
	zw, err := gzip.NewWriterLevel(&buf, flate.BestSpeed)
	if err != nil {
		return nil, err
	}
	// A freshly created gzip.Writer has the zero ModTime, which keeps the
	// stream header free of timestamps.
	if _, err := zw.Write(src); err != nil {
		return nil, err
	}
	if err := zw.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func (c *gzipCache) forArt(a *artifact.Artifact) ([]byte, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if b, ok := c.byArt[a]; ok {
		return b, nil
	}
	b, err := encodeGzip(a.Bytes())
	if err != nil {
		return nil, err
	}
	c.byArt[a] = b
	return b, nil
}

// selectRepresentation performs content selection on Accept-Encoding. Only
// identity and gzip are offered; ranges on the gzip representation index the
// encoded bytes, exactly as RFC 9110 permits (Content-Encoding is then sent
// alongside Content-Range).
func (s *Server) selectRepresentation(a *artifact.Artifact, acceptEncoding string) (representation, error) {
	identity := representation{
		data:        a.Bytes(),
		contentType: a.ContentType(),
		etag:        a.ETag(),
	}
	if !s.gzipEnabled {
		return identity, nil
	}
	if gzipWeight(acceptEncoding) <= 0 {
		return identity, nil
	}
	encoded, err := s.gzip.forArt(a)
	if err != nil {
		return representation{}, err
	}
	return representation{
		data:        encoded,
		contentType: a.ContentType(),
		etag:        artifact.Validator(encoded),
		encoding:    "gzip",
	}, nil
}

// gzipWeight returns the q-value for gzip in an Accept-Encoding header,
// defaulting to 0 when absent. "*: 0" is honored for the gzip token.
func gzipWeight(h string) float64 {
	if strings.TrimSpace(h) == "" {
		return 0
	}
	var gzipQ, starQ float64
	gzipSeen, starSeen := false, false
	for _, part := range strings.Split(h, ",") {
		token := strings.TrimSpace(part)
		name, params, _ := strings.Cut(token, ";")
		name = strings.ToLower(strings.TrimSpace(name))
		q := 1.0
		for _, p := range strings.Split(params, ";") {
			p = strings.TrimSpace(p)
			if v, ok := strings.CutPrefix(p, "q="); ok {
				if parsed, err := strconv.ParseFloat(strings.TrimSpace(v), 64); err == nil {
					q = parsed
				}
			}
		}
		switch name {
		case "gzip":
			gzipQ, gzipSeen = q, true
		case "*":
			starQ, starSeen = q, true
		}
	}
	if gzipSeen {
		return gzipQ
	}
	if starSeen {
		return starQ
	}
	return 0
}
