// Command seed loads deterministic demo data and prints API keys.
//
// It is idempotent: re-running it upserts catalog entities and leaves cost data
// untouched (import bills through the API instead).
package main

import (
	"context"
	"fmt"
	"log"
	"os"

	"costlens/internal/auth"
	"costlens/internal/config"

	"github.com/jackc/pgx/v5/pgxpool"
)

type entity struct {
	id, code, name string
}

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatal(err)
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatal(err)
	}
	defer pool.Close()

	const org1 = "11111111-1111-1111-1111-111111111111"
	const org2 = "22222222-2222-2222-2222-222222222222"
	const cc1 = "31111111-1111-1111-1111-111111111111"
	const cc2 = "32222222-2222-2222-2222-222222222222"
	const acct1 = "41111111-1111-1111-1111-111111111111"
	const acct2 = "41111112-1111-1111-1111-111111111111"
	const acct3 = "42222221-2222-2222-2222-222222222222"

	stmts := []string{
		`INSERT INTO organizations(id,code,name) VALUES
		 ($1,'org-alpha','Alpha Organization'),
		 ($2,'org-beta','Beta Organization')
		 ON CONFLICT (code) DO UPDATE SET name=EXCLUDED.name`,
		`INSERT INTO cost_centers(id,org_id,code,name) VALUES
		 ($1::uuid,$2::uuid,'cc-platform','Platform Engineering'),
		 ($3::uuid,$4::uuid,'cc-data','Data Platform')
		 ON CONFLICT (org_id,code) DO UPDATE SET name=EXCLUDED.name`,
		`INSERT INTO accounts(id,org_id,cost_center_id,code,name,created_at) VALUES
		 ($1::uuid,$2::uuid,$3::uuid,'acct-alpha-prod','Alpha prod cloud bill','2025-01-01'),
		 ($4::uuid,$2::uuid,$3::uuid,'acct-alpha-dev','Alpha dev cloud bill','2025-01-01'),
		 ($5::uuid,$6::uuid,$7::uuid,'acct-beta-prod','Beta prod cloud bill','2025-01-01')
		 ON CONFLICT (org_id,code) DO UPDATE SET name=EXCLUDED.name`,
		`INSERT INTO resources(code,service) VALUES
		 ('res-ec2-web','EC2'),('res-s3-data','S3'),('res-rds-main','RDS')
		 ON CONFLICT (code) DO UPDATE SET service=EXCLUDED.service`,
		`INSERT INTO app_users(id,login,full_name,role) VALUES
		 ('a1111111-1111-1111-1111-111111111111','admin','Ada Admin','admin'),
		 ('a2222222-2222-2222-2222-222222222222','analyst','Ana Analyst','analyst'),
		 ('a3333333-3333-3333-3333-333333333333','viewer','Vic Viewer','viewer'),
		 ('a4444444-4444-4444-4444-444444444444','analyst-beta','Bea Beta Analyst','analyst')
		 ON CONFLICT (login) DO UPDATE SET full_name=EXCLUDED.full_name, role=EXCLUDED.role`,
		`INSERT INTO user_org_scopes(user_id,org_id) VALUES
		 ('a2222222-2222-2222-2222-222222222222',$1::uuid),
		 ('a3333333-3333-3333-3333-333333333333',$1::uuid),
		 ('a4444444-4444-4444-4444-444444444444',$2::uuid)
		 ON CONFLICT DO NOTHING`,
	}
	args := [][]any{
		{org1, org2},
		{cc1, org1, cc2, org2},
		{acct1, org1, cc1, acct2, acct3, org2, cc2},
		nil,
		nil,
		{org1, org2},
	}
	for i, q := range stmts {
		if _, err := pool.Exec(ctx, q, args[i]...); err != nil {
			log.Fatalf("seed stmt %d: %v", i+1, err)
		}
	}

	keys := []struct {
		user, raw, hint string
	}{
		{"a1111111-1111-1111-1111-111111111111", "cl-sk-admin-0001", "admin key"},
		{"a2222222-2222-2222-2222-222222222222", "cl-sk-analyst-0001", "analyst alpha"},
		{"a3333333-3333-3333-3333-333333333333", "cl-sk-viewer-0001", "viewer alpha"},
		{"a4444444-4444-4444-4444-444444444444", "cl-sk-analyst-beta-0001", "analyst beta"},
	}
	for _, k := range keys {
		hash := auth.HashKey(k.raw)
		_, err := pool.Exec(ctx, `
			INSERT INTO api_keys(user_id,key_hash,hint) VALUES ($1,$2,$3)
			ON CONFLICT (key_hash) DO NOTHING`, k.user, hash, k.hint)
		if err != nil {
			log.Fatal(err)
		}
	}

	fmt.Fprintln(os.Stdout, "seed complete")
	fmt.Fprintf(os.Stdout, "org-alpha id = %s\n", org1)
	fmt.Fprintf(os.Stdout, "org-beta  id = %s\n", org2)
	fmt.Fprintf(os.Stdout, "cc-platform id = %s\n", cc1)
	fmt.Fprintf(os.Stdout, "acct-alpha-prod id = %s\n", acct1)
	for _, k := range keys {
		fmt.Fprintf(os.Stdout, "key %-14s %s  (Authorization: Bearer %s)\n", k.hint, k.raw, k.raw)
	}
}
