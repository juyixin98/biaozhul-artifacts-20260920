package db

import (
	"sort"
	"strings"

	"activityguard/migrations"

	"gorm.io/gorm"
)

// Migrate 按文件名顺序执行内嵌的全部 SQL 迁移。
// DDL 使用 CREATE TABLE IF NOT EXISTS，重复执行安全。
func Migrate(gdb *gorm.DB) error {
	entries, err := migrations.FS.ReadDir(".")
	if err != nil {
		return err
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".sql") {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)

	for _, name := range names {
		raw, err := migrations.FS.ReadFile(name)
		if err != nil {
			return err
		}
		for _, stmt := range splitStatements(string(raw)) {
			if err := gdb.Exec(stmt).Error; err != nil {
				return err
			}
		}
	}
	return nil
}

// splitStatements 是面向本项目迁移文件的朴素切分器：
// 按分号切分语句，去掉 -- 注释行与空白。迁移中不使用存储过程/触发器，
// 因此不需要完整 MySQL 词法分析。
func splitStatements(sql string) []string {
	var out []string
	var cur strings.Builder
	for _, line := range strings.Split(sql, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "--") {
			continue
		}
		cur.WriteString(line)
		cur.WriteByte('\n')
		if strings.HasSuffix(trimmed, ";") {
			stmt := strings.TrimSpace(cur.String())
			if stmt != "" && stmt != ";" {
				out = append(out, stmt)
			}
			cur.Reset()
		}
	}
	if stmt := strings.TrimSpace(cur.String()); stmt != "" {
		out = append(out, stmt)
	}
	return out
}
