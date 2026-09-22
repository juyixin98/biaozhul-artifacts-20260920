// Package migrations holds the SQL migration sources as an embedded FS so the
// migrator (and tests) can apply the schema from inside the binary.
package migrations

import "embed"

//go:embed *.sql
var FS embed.FS
