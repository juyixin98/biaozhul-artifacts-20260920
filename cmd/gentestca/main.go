// Command gentestca generates a self-signed ECDSA test CA and emits a
// Kubernetes Secret manifest on stdout containing tls.crt/tls.key.
//
// THIS IS A LOCAL SAMPLE CA ONLY. It is intentionally minimal and must never
// be used to issue certificates for anything but local development/testing.
// The private key is written solely into the emitted Secret's tls.key field;
// it is never logged.
package main

import (
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/example/cert-renewal-operator/internal/pki"
)

var generateCA = pki.GenerateTestCA
var serialString = pki.SerialString

func main() {
	var (
		namespace string
		name      string
		cn        string
		lifetime  time.Duration
	)
	flag.StringVar(&namespace, "namespace", "default", "namespace for the CA Secret")
	flag.StringVar(&name, "name", "test-ca", "name of the CA Secret")
	flag.StringVar(&cn, "common-name", "Local Test CA (sample only)", "CA subject CN")
	flag.DurationVar(&lifetime, "lifetime", 24*time.Hour, "CA lifetime")
	flag.Parse()

	certPEM, keyPEM, cert, _, err := generateCA(cn, time.Now(), lifetime)
	if err != nil {
		fmt.Fprintf(os.Stderr, "generate CA: %v\n", err)
		os.Exit(1)
	}

	fmt.Fprintf(os.Stderr, "generated sample CA: serial=%s notAfter=%s\n",
		serialString(cert.SerialNumber), cert.NotAfter.UTC().Format(time.RFC3339))

	out := fmt.Sprintf(`---
apiVersion: v1
kind: Secret
metadata:
  name: %s
  namespace: %s
  labels:
    certificates.example.com/component: sample-test-ca
  annotations:
    certificates.example.com/warning: "SAMPLE TEST CA ONLY - do not use outside local development"
type: kubernetes.io/tls
stringData:
  tls.crt: |
%s
  tls.key: |
%s
`, name, namespace, indent(string(certPEM)), indent(string(keyPEM)))
	fmt.Print(out)
}

func indent(s string) string {
	const pad = "    "
	out := ""
	for _, ch := range s {
		out += string(ch)
		if ch == '\n' {
			out += pad
		}
	}
	return pad + out
}
