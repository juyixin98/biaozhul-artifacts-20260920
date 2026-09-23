package store

import (
	"context"
	"os"
	"testing"
	"time"
)

// Integration test against a real PostgreSQL. Skipped unless
// GATEWAY_TEST_DSN is set (see Makefile target `test-integration`).
func testDSN(t *testing.T) string {
	t.Helper()
	dsn := os.Getenv("GATEWAY_TEST_DSN")
	if dsn == "" {
		t.Skip("GATEWAY_TEST_DSN not set; skipping PostgreSQL integration test")
	}
	return dsn
}

func TestAuditRoundTrip(t *testing.T) {
	dsn := testDSN(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	st, err := Connect(ctx, dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer st.Close()

	ddl, err := os.ReadFile("../../migrations/0001_init.sql")
	if err != nil {
		t.Fatal(err)
	}
	if err := st.Migrate(ctx, string(ddl)); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	if _, err := st.pool.Exec(ctx, `TRUNCATE conversion_audit`); err != nil {
		t.Fatal(err)
	}

	before, err := st.CountAudits(ctx)
	if err != nil {
		t.Fatal(err)
	}

	ok := AuditRecord{
		RecordID: "rec-ok-1", Direction: "v1->v2",
		SourceVersion: "telemetry/v1", TargetVersion: "telemetry/v2",
		ConverterVersion: "1.0.0", MappingVersion: "strict",
		Status: "ok", PayloadSHA256: "deadbeef",
	}
	if err := st.InsertAudit(ctx, ok); err != nil {
		t.Fatalf("insert ok: %v", err)
	}
	bad := AuditRecord{
		RecordID: "rec-err-1", Direction: "v2->v1",
		SourceVersion: "telemetry/v2", TargetVersion: "telemetry/v1",
		ConverterVersion: "1.0.0", MappingVersion: "strict",
		Status:        "error",
		ErrorDetail:   &ErrorDetail{FieldPath: "condition", Reason: "no v1 representation", RawValue: "MAINTENANCE(4)"},
		PayloadSHA256: "cafebabe",
	}
	if err := st.InsertAudit(ctx, bad); err != nil {
		t.Fatalf("insert error row: %v", err)
	}

	after, err := st.CountAudits(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if after-before != 2 {
		t.Fatalf("audits grew by %d, want 2", after-before)
	}

	// Verify the error detail round-tripped as JSONB.
	var fieldPath, raw string
	err = st.pool.QueryRow(ctx,
		`SELECT error_detail->>'field_path', error_detail->>'raw_value'
		 FROM conversion_audit WHERE record_id='rec-err-1'`).Scan(&fieldPath, &raw)
	if err != nil {
		t.Fatal(err)
	}
	if fieldPath != "condition" || raw != "MAINTENANCE(4)" {
		t.Errorf("error detail = (%q,%q)", fieldPath, raw)
	}
}
