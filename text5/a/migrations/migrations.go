// Package migrations embeds the SQL migration files so the server and the
// test suite can apply them without depending on the working directory.
package migrations

import "embed"

//go:embed *.sql
var FS embed.FS
