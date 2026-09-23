//go:build tools
// +build tools

// Package tools pins code-generation/test binaries via go.mod so builds are
// reproducible without a network-fetched @version at call time.
package tools

import (
	_ "sigs.k8s.io/controller-runtime/tools/setup-envtest"
	_ "sigs.k8s.io/controller-tools/cmd/controller-gen"
)
