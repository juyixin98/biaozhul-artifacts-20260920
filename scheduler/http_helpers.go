package scheduler

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
)

var errMethodNotAllowed = errors.New("method not allowed")

func notFoundErr(kind, id string) error {
	return fmt.Errorf("%w: %s %s", ErrTaskNotFound, kind, id)
}

func badRequestErr(msg string) error {
	return fmt.Errorf("%w: %s", ErrBadRequest, msg)
}

// decodeBody reads at most 1 MiB and rejects unknown / trailing fields.
func decodeBody(r *http.Request, v any) error {
	defer r.Body.Close()
	dec := json.NewDecoder(io.LimitReader(r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return fmt.Errorf("%w: %v", ErrBadRequest, err)
	}
	if dec.More() {
		return fmt.Errorf("%w: unexpected trailing JSON value", ErrBadRequest)
	}
	return nil
}
