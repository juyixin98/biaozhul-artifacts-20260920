// Package migrations 内嵌 SQL 迁移文件，保证迁移只有一份事实来源（Docker 与本地共用）。
package migrations

import "embed"

//go:embed *.sql
var FS embed.FS
