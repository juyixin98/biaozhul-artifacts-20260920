package integration

import (
	"context"
	"testing"

	"github.com/google/uuid"

	"github.com/clearsettle/clearsettle/internal/db"
	"github.com/clearsettle/clearsettle/internal/service"
)

// createMerchantWithKeys bootstraps a merchant and returns the merchant row,
// an operator actor and an auditor actor.
func (e *testEnv) createMerchantWithKeys(t *testing.T, name string) (db.Merchant, service.Actor, service.Actor) {
	t.Helper()
	ctx := context.Background()
	out, err := e.svc.CreateMerchant(ctx, e.admin, service.CreateMerchantInput{Name: name})
	if err != nil {
		t.Fatalf("create merchant: %v", err)
	}
	opActor, err := e.svc.Authenticate(ctx, out.OperatorKey)
	if err != nil {
		t.Fatalf("auth operator: %v", err)
	}
	issued, err := e.svc.IssueAPIKey(ctx, e.admin, service.IssueKeyInput{
		Role: "auditor", MerchantID: &out.Merchant.ID, Label: "ro",
	})
	if err != nil {
		t.Fatalf("issue auditor key: %v", err)
	}
	audActor, err := e.svc.Authenticate(ctx, issued.Key)
	if err != nil {
		t.Fatalf("auth auditor: %v", err)
	}
	return out.Merchant, opActor, audActor
}

func idem(key string) service.Idem {
	return service.Idem{Key: key, RequestHash: "h-" + key}
}

// captureAuth is a shorthand that authorizes + captures a payment.
func (e *testEnv) captureAuth(t *testing.T, actor service.Actor, mid uuid.UUID, amount int64) db.Transaction {
	t.Helper()
	ctx := context.Background()
	auth, _, err := e.svc.Authorize(ctx, actor, service.AuthorizeInput{
		MerchantID: mid, AmountCents: amount, CardLast4: "4242",
	}, idem("auth-"+uuid.NewString()))
	if err != nil {
		t.Fatalf("authorize: %v", err)
	}
	p, _, err := e.svc.Capture(ctx, actor, service.CaptureInput{PaymentID: auth.ID},
		idem("cap-"+uuid.NewString()))
	if err != nil {
		t.Fatalf("capture: %v", err)
	}
	return p
}

func struct2(mid uuid.UUID, amount int64) service.AuthorizeInput {
	return service.AuthorizeInput{MerchantID: mid, AmountCents: amount, CardLast4: "4242"}
}
func capIn(pid uuid.UUID, cents int64) service.CaptureInput {
	return service.CaptureInput{PaymentID: pid, CaptureCents: cents}
}
