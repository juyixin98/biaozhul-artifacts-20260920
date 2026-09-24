package webhook

import (
	"net/http"
	"time"

	"github.com/example/crd-migration-compat/internal/conversion"
)

// Options configures NewMux.
type Options struct {
	// Converter performs the actual v1alpha1 <-> v1 mapping. Required.
	Converter *conversion.Converter
	// ConversionTimeout bounds one object's conversion. Default 10s.
	ConversionTimeout time.Duration
	// Hooks are forwarded to a freshly built converter when Converter is nil.
	Hooks conversion.Hooks
}

// NewMux wires all webhook endpoints onto a fresh http.ServeMux:
//
//	POST /convert                                          conversion webhook
//	POST /mutate-migration-example-io-v1alpha1-task        v1alpha1 defaulting
//	POST /mutate-migration-example-io-v1-task              v1 defaulting
//	POST /validate-migration-example-io-v1alpha1-task      v1alpha1 validation
//	POST /validate-migration-example-io-v1-task            v1 validation
//	GET  /healthz                                          liveness probe
func NewMux(opts Options) http.Handler {
	c := opts.Converter
	if c == nil {
		c = conversion.NewConverter(opts.Hooks)
	}
	conv := NewConversionHandler(c, opts.ConversionTimeout)

	mux := http.NewServeMux()
	mux.Handle("/convert", conv)
	mux.Handle(pathMutateV1Alpha1, &admissionHandler{admit: mutateV1Alpha1})
	mux.Handle(pathMutateV1, &admissionHandler{admit: mutateV1})
	mux.Handle(pathValidateV1A1, &admissionHandler{admit: validateV1Alpha1})
	mux.Handle(pathValidateV1, &admissionHandler{admit: validateV1})
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("ok"))
	})
	return mux
}
