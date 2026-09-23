// Command sign computes the ingest HMAC-SHA256 signature for a JSON body.
// It prints the base64 signature to stdout, so it composes with curl.
//
// Usage:
//
//	go run ./cmd/sign -secret "$SECRET" -ts-us 1700000000000000 \
//	    -nonce "$(uuidgen)" -body-file body.json
package main

import (
	"flag"
	"fmt"
	"io"
	"os"

	"twap-service/internal/auth"
)

func main() {
	secret := flag.String("secret", "", "source secret, standard base64 (or set TWAP_SOURCE_SECRET)")
	ts := flag.String("ts-us", "", "request timestamp in microsecond epoch (or set TWAP_TS_US)")
	nonce := flag.String("nonce", "", "unique request nonce (or set TWAP_NONCE)")
	bodyFile := flag.String("body-file", "", "file containing the exact JSON body; '-' or empty for stdin")
	flag.Parse()

	if *secret == "" {
		*secret = os.Getenv("TWAP_SOURCE_SECRET")
	}
	if *ts == "" {
		*ts = os.Getenv("TWAP_TS_US")
	}
	if *nonce == "" {
		*nonce = os.Getenv("TWAP_NONCE")
	}
	if *secret == "" || *ts == "" || *nonce == "" {
		fmt.Fprintln(os.Stderr, "secret, ts-us and nonce are required")
		os.Exit(2)
	}
	var (
		body []byte
		err  error
	)
	switch *bodyFile {
	case "", "-":
		body, err = io.ReadAll(os.Stdin)
	default:
		body, err = os.ReadFile(*bodyFile)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	sig, err := auth.Sign(*secret, *ts, *nonce, body)
	if err != nil {
		fmt.Fprintln(os.Stderr, "sign:", err)
		os.Exit(1)
	}
	fmt.Print(sig)
}
