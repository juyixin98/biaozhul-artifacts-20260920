package client

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"mime"
	"mime/multipart"
	"strconv"
	"strings"
)

// RangePart is one reassembled part: bytes and the parsed Content-Range.
type RangePart struct {
	Start   int64
	End     int64
	Total   int64 // -1 when the server rendered total as "*"
	Payload []byte
}

// ErrMalformedMultipart signals a body that violates multipart/byteranges.
var ErrMalformedMultipart = errors.New("malformed multipart/byteranges body")

// parseContentRange parses "bytes start-end/total" (total may be "*").
func parseContentRange(v string) (start, end, total int64, err error) {
	const prefix = "bytes "
	if !strings.HasPrefix(v, prefix) {
		return 0, 0, 0, fmt.Errorf("%w: bad Content-Range %q", ErrMalformedMultipart, v)
	}
	rest := strings.TrimPrefix(v, prefix)
	rangePart, totalPart, ok := strings.Cut(rest, "/")
	if !ok {
		return 0, 0, 0, fmt.Errorf("%w: no slash in %q", ErrMalformedMultipart, v)
	}
	lo, hi, ok := strings.Cut(rangePart, "-")
	if !ok {
		return 0, 0, 0, fmt.Errorf("%w: no hyphen in %q", ErrMalformedMultipart, v)
	}
	if start, err = strconv.ParseInt(strings.TrimSpace(lo), 10, 64); err != nil {
		return 0, 0, 0, fmt.Errorf("%w: start: %v", ErrMalformedMultipart, err)
	}
	if end, err = strconv.ParseInt(strings.TrimSpace(hi), 10, 64); err != nil {
		return 0, 0, 0, fmt.Errorf("%w: end: %v", ErrMalformedMultipart, err)
	}
	switch strings.TrimSpace(totalPart) {
	case "*":
		total = -1
	case "":
		return 0, 0, 0, fmt.Errorf("%w: empty total", ErrMalformedMultipart)
	default:
		if total, err = strconv.ParseInt(strings.TrimSpace(totalPart), 10, 64); err != nil {
			return 0, 0, 0, fmt.Errorf("%w: total: %v", ErrMalformedMultipart, err)
		}
	}
	if start < 0 || end < start {
		return 0, 0, 0, fmt.Errorf("%w: inverted range %d-%d", ErrMalformedMultipart, start, end)
	}
	return start, end, total, nil
}

// boundaryFromContentType extracts the boundary of a multipart/byteranges CT.
func boundaryFromContentType(ct string) (string, error) {
	mediaType, params, err := mime.ParseMediaType(ct)
	if err != nil {
		return "", fmt.Errorf("%w: %v", ErrMalformedMultipart, err)
	}
	if !strings.HasPrefix(mediaType, "multipart/byteranges") {
		return "", fmt.Errorf("%w: unexpected media type %q", ErrMalformedMultipart, mediaType)
	}
	b := params["boundary"]
	if b == "" {
		return "", fmt.Errorf("%w: missing boundary", ErrMalformedMultipart)
	}
	return b, nil
}

// parseMultipart parses a multipart/byteranges body into ordered parts. It
// validates each part's Content-Range and rejects a payload whose length
// does not match the declared range.
func parseMultipart(body []byte, contentType string) ([]RangePart, error) {
	boundary, err := boundaryFromContentType(contentType)
	if err != nil {
		return nil, err
	}
	reader := multipart.NewReader(bytes.NewReader(body), boundary)
	var parts []RangePart
	for {
		part, err := reader.NextPart()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("%w: %v", ErrMalformedMultipart, err)
		}
		payload, err := io.ReadAll(part)
		if err != nil {
			return nil, fmt.Errorf("%w: payload: %v", ErrMalformedMultipart, err)
		}
		cr := part.Header.Get("Content-Range")
		if cr == "" {
			return nil, fmt.Errorf("%w: part missing Content-Range", ErrMalformedMultipart)
		}
		start, end, total, perr := parseContentRange(cr)
		if perr != nil {
			return nil, perr
		}
		if int64(len(payload)) != end-start+1 {
			return nil, fmt.Errorf(
				"%w: payload length %d != declared range length %d",
				ErrMalformedMultipart, len(payload), end-start+1)
		}
		parts = append(parts, RangePart{Start: start, End: end, Total: total, Payload: payload})
	}
	if len(parts) == 0 {
		return nil, fmt.Errorf("%w: no parts", ErrMalformedMultipart)
	}
	return parts, nil
}
