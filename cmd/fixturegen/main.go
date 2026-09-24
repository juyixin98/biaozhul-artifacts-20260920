// Command fixturegen writes a deterministic local OCI image layout for use
// with the /api/v1/import endpoint. It never touches the network.
//
//	go run ./cmd/fixturegen -out /tmp/oci-demo -case two-arch
package main

import (
	"flag"
	"fmt"
	"log"

	"ociarch/internal/fixture"
)

func main() {
	out := flag.String("out", "", "output directory for the OCI layout (required)")
	kase := flag.String("case", "two-arch",
		"fixture: two-arch | bad-config | missing-layer | ambiguous | arm-variants")
	flag.Parse()
	if *out == "" {
		log.Fatal("-out is required")
	}
	digest, err := fixture.Build(*out, *kase)
	if err != nil {
		log.Fatalf("build fixture: %v", err)
	}
	fmt.Printf("fixture %q written to %s\nroot digest: %s\n", *kase, *out, digest)
}
