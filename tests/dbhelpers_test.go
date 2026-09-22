package tests

import (
	"context"
	"testing"
)

func countRateAlerts(t *testing.T, env *testEnv, orgID, ruleID int64, version int32) int {
	t.Helper()
	var n int
	err := env.pool.QueryRow(context.Background(),
		`SELECT count(*) FROM alerts
		 WHERE org_id=$1 AND rule_id=$2 AND rule_version=$3 AND kind='rate'`,
		orgID, ruleID, version).Scan(&n)
	if err != nil {
		t.Fatalf("count rate alerts: %v", err)
	}
	return n
}

func firstRateAlertID(t *testing.T, env *testEnv, orgID, ruleID int64, version int32) int64 {
	t.Helper()
	var id int64
	err := env.pool.QueryRow(context.Background(),
		`SELECT id FROM alerts
		 WHERE org_id=$1 AND rule_id=$2 AND rule_version=$3 AND kind='rate'
		 ORDER BY id LIMIT 1`,
		orgID, ruleID, version).Scan(&id)
	if err != nil {
		t.Fatalf("get rate alert id: %v", err)
	}
	return id
}

func rateAlertCount(t *testing.T, env *testEnv, alertID int64) int32 {
	t.Helper()
	var n int32
	err := env.pool.QueryRow(context.Background(),
		`SELECT count(*) FROM alert_events WHERE alert_id=$1`, alertID).Scan(&n)
	if err != nil {
		t.Fatalf("count alert events: %v", err)
	}
	return n
}

func alertStatus(t *testing.T, env *testEnv, alertID int64) string {
	t.Helper()
	var s string
	err := env.pool.QueryRow(context.Background(),
		`SELECT status FROM alerts WHERE id=$1`, alertID).Scan(&s)
	if err != nil {
		t.Fatalf("alert status: %v", err)
	}
	return s
}

func alertVersion(t *testing.T, env *testEnv, alertID int64) int32 {
	t.Helper()
	var v int32
	err := env.pool.QueryRow(context.Background(),
		`SELECT version FROM alerts WHERE id=$1`, alertID).Scan(&v)
	if err != nil {
		t.Fatalf("alert version: %v", err)
	}
	return v
}

func alertRevisions(t *testing.T, env *testEnv, org, token string, alertID int64) []any {
	t.Helper()
	st, out := env.do(t, "GET",
		"/api/v1/orgs/"+org+"/alerts/"+itoa(alertID)+"/revisions", token, nil)
	env.mustStatus(t, st, 200, out)
	return asSlice(out["revisions"])
}

func itoa(i int64) string {
	const digits = "0123456789"
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var b [20]byte
	pos := len(b)
	for i > 0 {
		pos--
		b[pos] = digits[i%10]
		i /= 10
	}
	if neg {
		pos--
		b[pos] = '-'
	}
	return string(b[pos:])
}

func auditMaxSeq(t *testing.T, env *testEnv, orgID int64) int64 {
	t.Helper()
	var seq int64
	err := env.pool.QueryRow(context.Background(),
		`SELECT COALESCE(MAX(seq),0) FROM audit_entries WHERE org_id=$1`, orgID).Scan(&seq)
	if err != nil {
		t.Fatalf("max seq: %v", err)
	}
	return seq
}
