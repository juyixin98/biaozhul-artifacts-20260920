// Package seed embeds the sample-data SQL so the server can load it on first
// start (empty database) without depending on the working directory.
package seed

import "embed"

//go:embed seed.sql
var FS embed.FS
