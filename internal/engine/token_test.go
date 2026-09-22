package engine_test

import (
	"context"
	"strconv"
	"testing"

	"domainengine/internal/domains"
	"domainengine/internal/testsupport"
)

func itoa64(i int64) string { return strconv.FormatInt(i, 10) }

func domainsRenewReq(name string, reseller int64, years int, key *string) domains.RenewRequest {
	return domains.RenewRequest{Name: name, ResellerID: reseller, Years: years, IdempotencyKey: key}
}

// mintToken creates a principal of the given role and returns its plaintext
// bearer token. resellerID/customerID are ignored for admin.
func mintToken(t *testing.T, e *testsupport.Env, role string, resellerID, customerID int64) string {
	t.Helper()
	ctx := context.Background()
	switch role {
	case "admin":
		tok, created, err := e.Accounts.EnsureAdminPrincipal(ctx)
		if err != nil {
			t.Fatalf("ensure admin: %v", err)
		}
		if !created {
			t.Fatal("test requires a fresh admin principal; Reset truncated it")
		}
		return tok
	case "reseller":
		_, tok, err := e.Accounts.CreateReseller(ctx, "tok-r", 0)
		if err != nil {
			t.Fatal(err)
		}
		return tok
	}
	t.Fatalf("unsupported role %q", role)
	return ""
}
