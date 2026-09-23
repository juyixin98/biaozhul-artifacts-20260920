// Package migrations 内嵌数据库迁移 SQL。
package migrations

import "embed"

// FS 包含全部 *.sql 迁移文件，按文件名顺序应用。
//
//go:embed *.sql
var FS embed.FS
