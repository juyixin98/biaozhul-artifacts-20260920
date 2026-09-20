package api

import (
	"fmt"
	"net/http"
	"strings"
	"testing"
)

// TestImportRollback_OneBadItemRollsBackWholeBatch: the batch contains one
// invalid item (negative price). Nothing from the batch may persist.
func TestImportRollback_OneBadItemRollsBackWholeBatch(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	e.addDish(storeID, "EXISTING", "Already Here", 500, nil)

	body := `{"items":[
		{"sku":"NEW-1","name":"One","base_price":100},
		{"sku":"NEW-2","name":"Two","base_price":200},
		{"sku":"BAD","name":"Three","base_price":-5}
	]}`
	st, data := e.admin(http.MethodPost,
		fmt.Sprintf("/v1/admin/stores/%d/dishes/import", storeID), body)
	if st != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d %v", st, data)
	}

	// None of NEW-1/NEW-2/BAD should exist; EXISTING untouched.
	st, list := e.adminAny(http.MethodGet,
		fmt.Sprintf("/v1/admin/stores/%d/dishes", storeID), "")
	if st != http.StatusOK {
		t.Fatalf("list: %d", st)
	}
	skus := map[string]bool{}
	for _, it := range list.([]any) {
		skus[it.(map[string]any)["sku"].(string)] = true
	}
	for _, sku := range []string{"NEW-1", "NEW-2", "BAD"} {
		if skus[sku] {
			t.Fatalf("sku %s persisted despite failed batch (rollback broken)", sku)
		}
	}
	if !skus["EXISTING"] {
		t.Fatalf("pre-existing dish vanished after failed import")
	}
}

// TestImportUpsertsAndLimits validates the happy path: new dishes are inserted,
// same SKUs updated, all in one batch.
func TestImportUpsertsAndLimits(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	e.addDish(storeID, "DUP", "Old Name", 100, nil)

	body := `{"items":[
		{"sku":"DUP","name":"New Name","base_price":150,"daily_limit":10},
		{"sku":"FRESH","name":"Fresh","base_price":300}
	]}`
	st, data := e.admin(http.MethodPost,
		fmt.Sprintf("/v1/admin/stores/%d/dishes/import", storeID), body)
	if st != http.StatusOK {
		t.Fatalf("expected 200, got %d %v", st, data)
	}
	if n := int(data["imported"].(float64)); n != 2 {
		t.Fatalf("expected imported 2, got %d", n)
	}

	st, list := e.adminAny(http.MethodGet,
		fmt.Sprintf("/v1/admin/stores/%d/dishes", storeID), "")
	bySku := map[string]map[string]any{}
	for _, it := range list.([]any) {
		m := it.(map[string]any)
		bySku[m["sku"].(string)] = m
	}
	if bySku["DUP"]["name"] != "New Name" {
		t.Fatalf("upsert did not update name: %v", bySku["DUP"])
	}
	if int64(bySku["DUP"]["base_price"].(float64)) != 150 {
		t.Fatalf("upsert did not update price")
	}
	if bySku["FRESH"] == nil {
		t.Fatalf("new dish not inserted")
	}
}

// TestImportBatchSizeLimit enforces the 500-item cap before any write.
func TestImportBatchSizeLimit(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")

	var sb strings.Builder
	sb.WriteString(`{"items":[`)
	for i := 0; i < 501; i++ {
		if i > 0 {
			sb.WriteByte(',')
		}
		fmt.Fprintf(&sb, `{"sku":"S%03d","name":"n","base_price":1}`, i)
	}
	sb.WriteString(`]}`)

	st, data := e.admin(http.MethodPost,
		fmt.Sprintf("/v1/admin/stores/%d/dishes/import", storeID), sb.String())
	if st != http.StatusBadRequest {
		t.Fatalf("expected 400 for 501 items, got %d %v", st, data)
	}
}

// TestImportDuplicateSkuInBatchRejected ensures ambiguity is rejected.
func TestImportDuplicateSkuInBatchRejected(t *testing.T) {
	e := newTestEnv(t)
	storeID := e.createStoreViaAPI("UTC")
	body := `{"items":[
		{"sku":"X","name":"a","base_price":1},
		{"sku":"X","name":"b","base_price":2}
	]}`
	st, _ := e.admin(http.MethodPost,
		fmt.Sprintf("/v1/admin/stores/%d/dishes/import", storeID), body)
	if st != http.StatusBadRequest {
		t.Fatalf("expected 400 for duplicate sku, got %d", st)
	}
}
