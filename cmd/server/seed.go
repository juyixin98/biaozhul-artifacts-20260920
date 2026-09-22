package main

import (
	"context"
	"fmt"
	"math/rand"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/shopspring/decimal"

	"costlens/internal/csvparse"
	"costlens/internal/service"
)

// seedDemo creates two organizations with cost centers, accounts and users,
// then imports 90 days of generated billing through the real import path and
// finishes with a full rebuild. It is idempotent: rerunning skips creation and
// imports nothing new because every key already exists (counted as duplicate).
func seedDemo(ctx context.Context, pool *pgxpool.Pool) error {
	svc := service.New(pool)

	// --- catalog ---
	orgs := []struct{ ext, name string }{
		{"org-demo", "Demo Organization"},
		{"org-emea", "EMEA Organization"},
	}
	for _, o := range orgs {
		if _, err := svc.CreateOrg(ctx, service.CreateOrgInput{ExternalID: o.ext, Name: o.name}); err != nil {
			if !strings.Contains(err.Error(), "duplicate key") {
				return err
			}
		}
	}
	ccs := []struct {
		org, code, name string
	}{
		{"org-demo", "cc-platform", "Platform"},
		{"org-demo", "cc-data", "Data"},
		{"org-emea", "cc-emea-ops", "EMEA Operations"},
	}
	for _, c := range ccs {
		if _, err := svc.CreateCostCenter(ctx,
			service.CreateCostCenterInput{OrgExternalID: c.org, Code: c.code, Name: c.name}); err != nil {
			if !strings.Contains(err.Error(), "duplicate key") {
				return err
			}
		}
	}
	accounts := []struct {
		org, cc, ext, name string
	}{
		{"org-demo", "cc-platform", "acct-demo", "Demo compute account"},
		{"org-demo", "cc-data", "acct-data", "Demo data account"},
		{"org-emea", "cc-emea-ops", "acct-emea", "EMEA account (EUR)"},
	}
	for _, a := range accounts {
		if _, err := svc.CreateAccount(ctx, service.CreateAccountInput{
			OrgExternalID: a.org, CostCenterCode: a.cc,
			ExternalID: a.ext, Name: a.name,
		}); err != nil {
			if !strings.Contains(err.Error(), "duplicate key") {
				return err
			}
		}
	}

	// --- users (tokens only shown on first creation) ---
	users := []struct {
		username, role string
		grants         []string
	}{
		{"admin", "admin", nil},
		{"analyst-demo", "analyst", []string{"org-demo"}},
		{"viewer-all", "viewer", []string{"org-demo", "org-emea"}},
	}
	for _, u := range users {
		_, token, err := svc.CreateUser(ctx, service.CreateUserInput{Username: u.username, Role: u.role})
		if err != nil {
			if !strings.Contains(err.Error(), "duplicate key") {
				return err
			}
		} else {
			fmt.Printf("seed user %-14s token: %s\n", u.username, token)
			for _, g := range u.grants {
				if err := svc.Grant(ctx, u.username, g); err != nil {
					return err
				}
			}
		}
	}

	// --- budgets for current month ---
	var today time.Time
	if err := pool.QueryRow(ctx, `SELECT CURRENT_DATE`).Scan(&today); err != nil {
		return err
	}
	month := today.Format("2006-01")
	// cost center id lookup
	ccID := func(code string) int64 {
		var id int64
		_ = pool.QueryRow(ctx, `SELECT id FROM cost_centers WHERE code=$1`, code).Scan(&id)
		return id
	}
	if _, err := svc.SetBudget(ctx, service.SetBudgetInput{
		CostCenterID: ccID("cc-platform"), Currency: "USD",
		Month: month, MonthlyLimit: "600.00",
	}, 0); err != nil && !strings.Contains(err.Error(), "duplicate") {
		// re-setting is allowed (creates a version); error here only on real failure
		return err
	}

	// --- billing history: two resources, USD, last 90 days ---
	if err := importHistory(ctx, svc, "acct-demo", "USD", today, 90); err != nil {
		return err
	}
	if err := importHistory(ctx, svc, "acct-emea", "EUR", today, 60); err != nil {
		return err
	}
	if _, err := svc.Rebuild(ctx, 0); err != nil {
		return err
	}
	return nil
}

func importHistory(ctx context.Context, svc *service.Service, account, ccy string,
	today time.Time, days int) error {
	rng := rand.New(rand.NewSource(int64(days) * 7))
	start := today.AddDate(0, 0, -days)
	var rows []csvparse.Row
	line := 2
	for d := start; d.Before(today); d = d.AddDate(0, 0, 1) {
		// Resource r1: stable ~10.00 with small noise; two zero-spend days per
		// week are skipped entirely to produce genuine zero observations.
		if d.Weekday() != time.Sunday {
			noise := decimal.NewFromFloat(float64(rng.Intn(50)) / 100) // 0.00-0.49
			amt := decimal.RequireFromString("10.00").Add(noise)
			rows = append(rows, csvparse.Row{
				Line: line, ResourceID: "r1", Service: "compute",
				Date: d, Currency: ccy, Amount: amt,
			})
			line++
		}
		// Resource r2: occasional small storage charge.
		if rng.Intn(3) == 0 {
			rows = append(rows, csvparse.Row{
				Line: line, ResourceID: "r2", Service: "storage",
				Date: d, Currency: ccy,
				Amount: decimal.RequireFromString("2.50"),
			})
			line++
		}
	}
	// A large spike yesterday on r1 (anomalous under mean+2*sigma).
	yesterday := today.AddDate(0, 0, -1)
	rows = append(rows, csvparse.Row{
		Line: line, ResourceID: "r3", Service: "compute",
		Date: yesterday, Currency: ccy,
		Amount: decimal.RequireFromString("99.00"),
	})

	const chunk = 4000
	for i := 0; i < len(rows); i += chunk {
		end := i + chunk
		if end > len(rows) {
			end = len(rows)
		}
		if _, err := svc.Import(ctx, account, "seed.csv", 0, rows[i:end]); err != nil {
			return err
		}
	}
	return nil
}
