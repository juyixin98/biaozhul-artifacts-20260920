// Command gen-test-ca generates an ephemeral test CA and prints a Kubernetes
// TLS Secret manifest on stdout.
//
// WARNING: sample-grade material for local development only. Never use this
// CA to protect real traffic.
package main

import (
	"fmt"
	"os"
	"time"

	"encoding/base64"
	"github.com/example/certrenewal/internal/clock"
	"github.com/example/certrenewal/internal/pki"
)

func main() {
	name := "test-ca"
	namespace := "default"
	if len(os.Args) > 1 {
		name = os.Args[1]
	}
	if len(os.Args) > 2 {
		namespace = os.Args[2]
	}
	certPEM, keyPEM, err := pki.GenerateTestCA(clock.System{}.Now(), 365*24*time.Hour)
	if err != nil {
		fmt.Fprintln(os.Stderr, "generate CA:", err)
		os.Exit(1)
	}
	fmt.Printf(`apiVersion: v1
kind: Secret
metadata:
  name: %s
  namespace: %s
  labels:
    app.kubernetes.io/part-of: cert-renewal-coordinator
type: kubernetes.io/tls
data:
  tls.crt: %s
  tls.key: %s
  ca.crt: %s
`, name, namespace,
		base64.StdEncoding.EncodeToString(certPEM),
		base64.StdEncoding.EncodeToString(keyPEM),
		base64.StdEncoding.EncodeToString(certPEM))
}
