package engine_test

import (
	"context"
	"testing"

	"domainengine/internal/domains"
	"domainengine/internal/models"
	"domainengine/internal/testsupport"
)

func domainsReq(name string, reseller, customer int64) domains.RegisterRequest {
	return domains.RegisterRequest{
		Name: name, CustomerID: customer, ResellerID: reseller, Years: 1,
	}
}

func mustDomain(t *testing.T, e *testsupport.Env, name string) *models.Domain {
	t.Helper()
	d, err := e.Domains.Get(context.Background(), name)
	if err != nil {
		t.Fatalf("get domain %s: %v", name, err)
	}
	return d
}
