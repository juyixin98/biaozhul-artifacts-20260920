package database

import (
	"embed"
	"fmt"
	"sort"
	"strings"
	"time"

	"gorm.io/gorm"
)

//go:embed migrations/*.sql
var migrationFS embed.FS

// Migrate applies every embedded SQL migration not yet recorded in
// schema_migrations, each in its own transaction. DDL statements are split and
// executed one by one because the MySQL driver disallows multi-statement
// Exec() unless multiStatements is enabled.
func Migrate(db *gorm.DB) error {
	if err := db.Exec(`CREATE TABLE IF NOT EXISTS schema_migrations (
		version VARCHAR(255) NOT NULL PRIMARY KEY,
		applied_at DATETIME(6) NOT NULL
	) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`).Error; err != nil {
		return fmt.Errorf("ensure schema_migrations: %w", err)
	}

	entries, err := migrationFS.ReadDir("migrations")
	if err != nil {
		return err
	}
	var names []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".sql") {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)

	for _, name := range names {
		var exists int64
		if err := db.Table("schema_migrations").
			Where("version = ?", name).
			Count(&exists).Error; err != nil {
			return err
		}
		if exists > 0 {
			continue
		}
		sqlBytes, err := migrationFS.ReadFile("migrations/" + name)
		if err != nil {
			return err
		}
		stmts := splitStatements(string(sqlBytes))
		err = db.Transaction(func(tx *gorm.DB) error {
			for _, stmt := range stmts {
				if err := tx.Exec(stmt).Error; err != nil {
					return err
				}
			}
			return tx.Exec(
				"INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
				name, time.Now().UTC(),
			).Error
		})
		if err != nil {
			return fmt.Errorf("apply migration %s: %w", name, err)
		}
	}
	return nil
}

// splitStatements splits a MySQL DDL script on semicolons that end a line,
// stripping -- comments and blank lines. The bundled migrations contain no
// semicolons inside string literals, so a small lexer is sufficient and stays
// quote-aware for safety.
func splitStatements(script string) []string {
	var out []string
	var cur strings.Builder
	flush := func() {
		s := strings.TrimSpace(cur.String())
		if s != "" {
			out = append(out, s)
		}
		cur.Reset()
	}
	inSingle, inDouble, inBacktick := false, false, false
	for i := 0; i < len(script); i++ {
		ch := script[i]
		switch ch {
		case '\'':
			if !inDouble && !inBacktick {
				inSingle = !inSingle
			}
		case '"':
			if !inSingle && !inBacktick {
				inDouble = !inDouble
			}
		case '`':
			if !inSingle && !inDouble {
				inBacktick = !inBacktick
			}
		case ';':
			if !inSingle && !inDouble && !inBacktick {
				flush()
				continue
			}
		}
		cur.WriteByte(ch)
	}
	flush()

	// Strip -- line comments from each statement.
	for i, s := range out {
		var b strings.Builder
		for _, line := range strings.Split(s, "\n") {
			trimmed := strings.TrimSpace(line)
			if strings.HasPrefix(trimmed, "--") {
				continue
			}
			b.WriteString(line)
			b.WriteByte('\n')
		}
		out[i] = strings.TrimSpace(b.String())
	}
	return out
}

// WaitForDB pings MySQL until it answers or retries are exhausted.
func WaitForDB(db *gorm.DB, retries int, delay time.Duration) error {
	var lastErr error
	for i := 0; i < retries; i++ {
		sqlDB, err := db.DB()
		if err == nil {
			if err = sqlDB.Ping(); err == nil {
				return nil
			}
			lastErr = err
		} else {
			lastErr = err
		}
		time.Sleep(delay)
	}
	return lastErr
}
