package api

import (
	"errors"
	"io"
	"net/http"
	"path/filepath"
	"strings"
)

// readUpload accepts either:
//   - a raw body with Content-Type text/csv (filename via X-Filename header), or
//   - a multipart/form-data upload containing a "file" field.
//
// The body is capped at 32 MiB; the returned ReadCloser must be closed.
func readUpload(w http.ResponseWriter, r *http.Request) (string, io.ReadCloser, error) {
	const maxBytes = 32 << 20
	ct := r.Header.Get("Content-Type")
	switch {
	case strings.HasPrefix(ct, "multipart/form-data"):
		if err := r.ParseMultipartForm(maxBytes); err != nil {
			return "", nil, errors.New("cannot parse multipart form: " + err.Error())
		}
		f, fh, err := r.FormFile("file")
		if err != nil {
			return "", nil, errors.New("multipart form must include a 'file' field")
		}
		name := filepath.Base(fh.Filename)
		if !strings.HasSuffix(strings.ToLower(name), ".csv") {
			f.Close()
			return "", nil, errors.New("uploaded file must have a .csv extension")
		}
		return name, http.MaxBytesReader(w, f, maxBytes), nil
	default:
		name := r.Header.Get("X-Filename")
		if name == "" {
			name = "upload.csv"
		}
		name = filepath.Base(name)
		return name, http.MaxBytesReader(w, r.Body, maxBytes), nil
	}
}
