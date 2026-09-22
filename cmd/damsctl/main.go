// Command damsctl manages local-test DAMS data:
//
//	damsctl seed      create demo orgs/users/rules and print bearer tokens
//	damsctl migrate   apply database migrations and exit
//
// It never connects to any monitored database and never executes uploaded
// SQL; it only talks to the DAMS metadata database.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"dams.local/dams/internal/auth"
	"dams.local/dams/internal/config"
	"dams.local/dams/internal/migrate"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	cfg := config.Load()
	ctx := context.Background()

	switch os.Args[1] {
	case "migrate":
		conn, err := pgx.Connect(ctx, cfg.DatabaseURL)
		must(err)
		must(migrate.EnsureSchema(ctx, conn))
		fmt.Println("migrations applied")
	case "seed":
		fs := flag.NewFlagSet("seed", flag.ExitOnError)
		replace := fs.Bool("replace", false, "delete existing demo data first")
		_ = fs.Parse(os.Args[2:])
		runSeed(ctx, cfg.DatabaseURL, *replace)
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: damsctl [seed [-replace] | migrate]")
	os.Exit(2)
}

func must(err error) {
	if err != nil {
		log.Fatal(err)
	}
}

type seededToken struct {
	Email string `json:"email"`
	Name  string `json:"name"`
	Org   string `json:"org"`
	Role  string `json:"role"`
	Token string `json:"token"`
}

func runSeed(ctx context.Context, dbURL string, replace bool) {
	// Seeding applies migrations itself, so the one-shot job does not depend
	// on the API container having started and applied them first.
	conn, err := pgx.Connect(ctx, dbURL)
	must(err)
	must(migrate.EnsureSchema(ctx, conn))
	_ = conn.Close(ctx)

	pool, err := pgxpool.New(ctx, dbURL)
	must(err)
	defer pool.Close()

	if replace {
		_, err = pool.Exec(ctx, `TRUNCATE audit_entries, alert_revisions, alert_events,
			alerts, events, rules, export_policies, sources,
			api_tokens, memberships, users, organizations RESTART IDENTITY CASCADE`)
		must(err)
	}

	type userSpec struct {
		email, name string
		orgs        map[string]string // org slug -> role
	}
	specs := []userSpec{
		{"ada@dams.local", "Ada Admin", map[string]string{
			"demo": "admin",
		}},
		{"ana@dams.local", "Ana Analyst", map[string]string{
			"demo": "analyst",
		}},
		{"audra@dams.local", "Audra Auditor", map[string]string{
			"demo": "auditor",
		}},
		// Analyst in a *different* org, used by the cross-organization
		// authorization tests.
		{"oliver@other.local", "Oliver OtherOrg", map[string]string{
			"other": "analyst",
		}},
		{"admin@other.local", "Admin OtherOrg", map[string]string{
			"other": "admin",
		}},
	}

	var tokens []seededToken

	tx, err := pool.Begin(ctx)
	must(err)
	defer tx.Rollback(ctx)

	orgs := map[string]int64{}
	for _, slug := range []string{"demo", "other"} {
		tz := "Asia/Shanghai"
		if slug == "other" {
			tz = "UTC"
		}
		var id int64
		err = tx.QueryRow(ctx,
			`INSERT INTO organizations (slug, name, timezone)
			 VALUES ($1,$2,$3)
			 ON CONFLICT (slug) DO UPDATE SET name=EXCLUDED.name, timezone=EXCLUDED.timezone
			 RETURNING id`, slug, slug+" organization", tz).Scan(&id)
		must(err)
		orgs[slug] = id
	}

	for _, u := range specs {
		var uid int64
		err = tx.QueryRow(ctx,
			`INSERT INTO users (email, display_name) VALUES ($1,$2)
			 ON CONFLICT (email) DO UPDATE SET display_name=EXCLUDED.display_name
			 RETURNING id`, u.email, u.name).Scan(&uid)
		must(err)
		for slug, role := range u.orgs {
			_, err = tx.Exec(ctx,
				`INSERT INTO memberships (org_id, user_id, role) VALUES ($1,$2,$3)
				 ON CONFLICT (org_id, user_id) DO UPDATE SET role=EXCLUDED.role`,
				orgs[slug], uid, role)
			must(err)
		}
		token, err := auth.GenerateToken()
		must(err)
		_, err = tx.Exec(ctx,
			`INSERT INTO api_tokens (user_id, token_hash, label)
			 VALUES ($1,$2,'seed') ON CONFLICT DO NOTHING`,
			uid, auth.HashToken(token))
		must(err)
		for slug, role := range u.orgs {
			tokens = append(tokens, seededToken{
				Email: u.email, Name: u.name, Org: slug, Role: role, Token: token,
			})
		}
	}

	// Default rules for the demo org.
	demo := orgs["demo"]
	_, err = tx.Exec(ctx, `
		INSERT INTO rules (id, org_id, version, kind, name, enabled,
		                   window_seconds, max_events, tables, hour_start, hour_end)
		VALUES
		(nextval('rules_id_seq'), $1, 1, 'rate', '500 events / 5 minutes per user',
		 TRUE, 300, 500, '{}', 6, 20),
		(nextval('rules_id_seq'), $1, 1, 'sensitive', 'off-hours sensitive tables',
		 TRUE, 300, 500,
		 ARRAY['public.employee_salary','public.customer_pii','customer_pii','employee_salary'],
		 6, 20)`, demo)
	must(err)

	_, err = tx.Exec(ctx,
		`INSERT INTO export_policies (org_id, masked_fields)
		 VALUES ($1, ARRAY['db_user','table_name'])
		 ON CONFLICT (org_id) DO NOTHING`, demo)
	must(err)

	must(tx.Commit(ctx))

	out, _ := json.MarshalIndent(tokens, "", "  ")
	fmt.Println(string(out))
	fmt.Fprintln(os.Stderr, "tokens shown once; re-run 'damsctl seed -replace' to rotate")
}
