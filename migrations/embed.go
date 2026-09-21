// Package migrations holds the SQL migration files and embeds them into the
// binary so deployments need no external migration assets.
package migrations

import "embed"

// FS contains all *.sql migration files at build time.
//
//go:embed *.sql
var FS embed.FS
