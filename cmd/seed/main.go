// Seed creates a demo organization, four role-scoped API keys and the two
// default rule versions. It prints the raw keys ONCE — only hashes are
// stored. Safe to re-run: existing rows are left untouched.
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"time"

	"dams/internal/db"
	"dams/internal/platform/auth"
	"dams/internal/platform/dbpool"
	"dams/internal/platform/migrate"

	"github.com/jackc/pgx/v5/pgtype"
)

func main() {
	dsn := envOr("DAMS_DATABASE_URL", "postgres://dams:dams@localhost:5432/dams?sslmode=disable")
	orgName := flag.String("org", "acme", "organization name")
	tz := flag.String("tz", "Asia/Shanghai", "organization timezone (IANA)")
	flag.Parse()

	if _, err := time.LoadLocation(*tz); err != nil {
		log.Fatalf("bad timezone %q: %v", *tz, err)
	}

	ctx := context.Background()
	pool, err := dbpool.New(ctx, dsn)
	if err != nil {
		log.Fatalf("connect db: %v", err)
	}
	defer pool.Close()
	if _, err := migrate.Up(ctx, pool); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	q := db.New(pool)

	org, err := q.GetOrganizationByName(ctx, *orgName)
	if err != nil {
		org, err = q.CreateOrganization(ctx, db.CreateOrganizationParams{
			Name: *orgName, Timezone: *tz,
		})
		if err != nil {
			log.Fatal(err)
		}
	}

	roles := []struct{ name, role string }{
		{"admin", "admin"},
		{"analyst", "analyst"},
		{"auditor", "auditor"},
		{"collector", "collector"},
	}
	type issued struct{ name, key string }
	var issuedKeys []issued
	for _, r := range roles {
		raw := "dams_" + r.role + "_" + randHex(20)
		if _, err := q.CreateAPIKey(ctx, db.CreateAPIKeyParams{
			KeyHash:   auth.HashKey(raw),
			KeyPrefix: raw[:18],
			OrgID:     org.ID,
			Name:      "seed-" + r.name,
			Role:      r.role,
		}); err != nil {
			// unique name etc. — skip but still show only new keys
			continue
		}
		issuedKeys = append(issuedKeys, issued{r.name, raw})
	}

	// Default rules (only if the org has none).
	existing, _ := q.ListRuleVersions(ctx, db.ListRuleVersionsParams{
		OrgID: org.ID, RuleType: "frequency",
	})
	if len(existing) == 0 {
		freq, _ := json.Marshal(map[string]any{
			"window_seconds": 300, "threshold": 500,
			"actions": []string{"select", "insert", "update", "delete", "ddl", "grant"},
		})
		if _, err := q.CreateRuleVersion(ctx, db.CreateRuleVersionParams{
			OrgID: org.ID, RuleType: "frequency", Version: 1, IsActive: true,
			Params: freq, EffectiveAt: pgtype.Timestamptz{Time: time.Unix(0, 0).UTC(), Valid: true},
		}); err != nil {
			log.Printf("seed frequency rule: %v", err)
		}
		sens, _ := json.Marshal(map[string]any{
			"sensitive_tables": []map[string]string{
				{"schema": "public", "table": "salaries"},
				{"schema": "hr", "table": "employees"},
			},
			"allowed_start_hour": 6,
			"allowed_end_hour":   20,
			"actions":            []string{"select"},
		})
		if _, err := q.CreateRuleVersion(ctx, db.CreateRuleVersionParams{
			OrgID: org.ID, RuleType: "sensitive_hours", Version: 1, IsActive: true,
			Params: sens, EffectiveAt: pgtype.Timestamptz{Time: time.Unix(0, 0).UTC(), Valid: true},
		}); err != nil {
			log.Printf("seed sensitive rule: %v", err)
		}
	}

	fmt.Printf("organization: %s (id=%d, tz=%s)\n", org.Name, org.ID, org.Timezone)
	if len(issuedKeys) == 0 {
		fmt.Println("all seed keys already existed; no new keys issued")
		return
	}
	fmt.Println("new API keys (store now, only hashes are retained):")
	for _, k := range issuedKeys {
		fmt.Printf("  %-10s %s\n", k.name, k.key)
	}
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func randHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
