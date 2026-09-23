package service

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sort"
	"time"

	"costlens/internal/auth"
	"costlens/internal/csvio"
	"costlens/internal/db/dbgen"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/shopspring/decimal"
)

// ImportResult is the outcome of a successful batch.
type ImportResult struct {
	ImportID          string    `json:"import_id"`
	RawRowCount       int       `json:"raw_row_count"`
	InsertedRowCount  int       `json:"inserted_row_count"`
	DuplicateRowCount int       `json:"duplicate_row_count"`
	MinDate           time.Time `json:"min_date"`
	MaxDate           time.Time `json:"max_date"`
	Accounts          []string  `json:"accounts"`
}

// Import parses and loads one CSV bill batch into one organization.
//
// The whole batch is atomic: any parse/conflict/validation error rolls back
// every data change and the returned error's Line is the 1-based CSV line. A
// failed-import audit row is still persisted (in a separate transaction), so
// rejected batches are visible in /imports.
func (s *Service) Import(ctx context.Context, p *auth.Principal, orgID string, filename string, r io.Reader) (*ImportResult, error) {
	if !p.CanImport() {
		return nil, &ValidationError{Code: "forbidden", Message: "role may not import bills"}
	}
	if !p.CanAccessOrg(orgID) {
		return nil, &ValidationError{Code: "forbidden_org", Message: "organization not in user scope"}
	}

	rows, err := csvio.Parse(r)
	if err != nil {
		var pe *csvio.ParseError
		if errors.As(err, &pe) {
			ve := &ValidationError{Line: pe.Line, Code: "csv_parse", Message: pe.Error()}
			s.recordFailedImport(ctx, p, orgID, filename, len(rows), ve)
			return nil, ve
		}
		ve := &ValidationError{Line: 0, Code: "csv_parse", Message: err.Error()}
		s.recordFailedImport(ctx, p, orgID, filename, 0, ve)
		return nil, ve
	}
	res, txErr := s.runImportTx(ctx, p, orgID, filename, rows)
	if txErr != nil {
		var ve *ValidationError
		if errors.As(txErr, &ve) {
			s.recordFailedImport(ctx, p, orgID, filename, len(rows), ve)
		}
		return nil, txErr
	}
	return res, nil
}

type resolvedRow struct {
	line       int
	accountID  string
	resourceID string
	service    string
	date       time.Time
	currency   string
	amount     decimal.Decimal
}

type costKey struct {
	account  string
	resource string
	date     string
}

func (k costKey) String() string {
	return fmt.Sprintf("account=%s resource=%s date=%s", k.account, k.resource, k.date)
}

// batchFailure is a validation failure that must both roll back the data
// transaction and be persisted as a failed-import audit row afterwards.
type batchFailure struct{ ve *ValidationError }

func (e *batchFailure) Error() string { return e.ve.Error() }

func fail(line int, code, msg string) error {
	return &batchFailure{ve: &ValidationError{Line: line, Code: code, Message: msg}}
}

func (s *Service) runImportTx(ctx context.Context, p *auth.Principal, orgID, filename string, rows []csvio.Row) (*ImportResult, error) {
	var result *ImportResult
	err := s.transact(ctx, func(tx pgx.Tx) error {
		q := dbgen.New(tx)

		// Serialize all mutating pipelines against each other.
		if err := q.AcquireTxAdvisoryLock(ctx, advisoryLockKey); err != nil {
			return err
		}

		// ---- Resolve accounts within the authorized org ----
		acctCodes := uniqueCodes(len(rows), func(i int) string { return rows[i].AccountCode })
		accounts, err := q.GetAccountsByCodesScoped(ctx, dbgen.GetAccountsByCodesScopedParams{
			Column1: []string{orgID},
			Column2: acctCodes,
		})
		if err != nil {
			return err
		}
		acctByCode := make(map[string]dbgen.Account, len(accounts))
		for _, a := range accounts {
			acctByCode[a.Code] = a
		}

		// ---- Resolve / auto-register resources ----
		resCodes := uniqueCodes(len(rows), func(i int) string { return rows[i].ResourceID })
		existing, err := q.GetResourcesByCodes(ctx, resCodes)
		if err != nil {
			return err
		}
		resByCode := make(map[string]dbgen.Resource, len(existing))
		for _, rr := range existing {
			resByCode[rr.Code] = rr
		}
		serviceByCode := map[string]string{}
		for _, rw := range rows {
			if _, ok := serviceByCode[rw.ResourceID]; !ok {
				serviceByCode[rw.ResourceID] = rw.Service
			}
		}
		for _, code := range resCodes {
			if _, ok := resByCode[code]; !ok {
				rr, err := q.UpsertResource(ctx, dbgen.UpsertResourceParams{
					Code: code, Service: serviceByCode[code],
				})
				if err != nil {
					return err
				}
				resByCode[code] = rr
			}
		}

		// ---- Resolve rows + intra-batch duplicate keys ----
		resolved := make([]resolvedRow, 0, len(rows))
		seen := make(map[costKey]int, len(rows)) // key -> first line
		minD, maxD := truncDate(rows[0].CostDate), truncDate(rows[0].CostDate)
		acctSet := map[string]bool{}
		ccSet := map[string]bool{}
		for _, rw := range rows {
			acct, ok := acctByCode[rw.AccountCode]
			if !ok {
				return fail(rw.Line, "unknown_account",
					"account code not found in authorized organization: "+rw.AccountCode)
			}
			res := resByCode[rw.ResourceID]
			// A resource code is global catalog data with one service; a row
			// claiming a different service for a known resource is a conflict.
			if res.Service != rw.Service {
				return fail(rw.Line, "content_conflict",
					fmt.Sprintf("resource %s is registered as service %q, file says %q",
						rw.ResourceID, res.Service, rw.Service))
			}
			d := truncDate(rw.CostDate)
			if d.Before(minD) {
				minD = d
			}
			if d.After(maxD) {
				maxD = d
			}
			acctSet[acct.ID] = true
			ccSet[acct.CostCenterID] = true
			key := costKey{account: acct.ID, resource: res.ID, date: dateStr(d)}
			if firstLine, dup := seen[key]; dup {
				return fail(rw.Line, "duplicate_key_in_file",
					fmt.Sprintf("duplicate key in file (first seen at line %d): %s", firstLine, key.String()))
			}
			seen[key] = rw.Line
			resolved = append(resolved, resolvedRow{
				line: rw.Line, accountID: acct.ID, resourceID: res.ID,
				service: rw.Service, date: d, currency: rw.Currency, amount: rw.Amount,
			})
		}

		// ---- Compare against committed rows (before any audit row is written) ----
		accountIDs := mapKeys(acctSet)
		existingRows, err := q.GetExistingCostsForKeys(ctx, dbgen.GetExistingCostsForKeysParams{
			CostDate:   minD,
			CostDate_2: maxD,
			Column3:    accountIDs,
		})
		if err != nil {
			return err
		}
		existingByKey := make(map[costKey]dbgen.GetExistingCostsForKeysRow, len(existingRows))
		for _, er := range existingRows {
			existingByKey[costKey{
				account: er.AccountID, resource: er.ResourceID, date: dateStr(er.CostDate),
			}] = er
		}

		toInsert := make([]resolvedRow, 0, len(resolved))
		dupCount := 0
		for _, rr := range resolved {
			key := costKey{account: rr.accountID, resource: rr.resourceID, date: dateStr(rr.date)}
			if old, ok := existingByKey[key]; ok {
				if old.Currency != rr.currency || !old.Amount.Equal(rr.amount) || old.Service != rr.service {
					return fail(rr.line, "content_conflict",
						fmt.Sprintf("existing bill for %s differs (have %s %s service=%q, file says %s %s service=%q)",
							key.String(),
							old.Currency, old.Amount.String(), old.Service,
							rr.currency, rr.amount.String(), rr.service))
				}
				dupCount++
				continue
			}
			toInsert = append(toInsert, rr)
		}

		// Batch content is settled. Persist the import audit row inside the
		// same transaction so costs can reference it.
		imp, err := q.CreateImport(ctx, dbgen.CreateImportParams{
			OrgID:        textVal(orgID),
			UserID:       textVal(p.ID),
			Filename:     filename,
			Status:       dbgen.ImportStatusSucceeded,
			RawRowCount:  int32(len(rows)),
			RowCount:     int32(len(toInsert)),
			ErrorLine:    pgtype.Int4{Valid: false},
			ErrorCode:    pgtype.Text{Valid: false},
			ErrorMessage: pgtype.Text{Valid: false},
		})
		if err != nil {
			return err
		}

		// ---- Insert new cost rows (bulk, fixed-point text encoding) ----
		if len(toInsert) > 0 {
			params := dbgen.InsertCostsParams{
				Column1: imp.ID,
				Column2: make([]string, len(toInsert)),
				Column3: make([]string, len(toInsert)),
				Column4: make([]string, len(toInsert)),
				Column5: make([]string, len(toInsert)),
				Column6: make([]string, len(toInsert)),
				Column7: make([]string, len(toInsert)),
			}
			for i, rr := range toInsert {
				params.Column2[i] = rr.accountID
				params.Column3[i] = rr.resourceID
				params.Column4[i] = rr.service
				params.Column5[i] = dateStr(rr.date)
				params.Column6[i] = rr.currency
				params.Column7[i] = rr.amount.String()
			}
			if err := q.InsertCosts(ctx, params); err != nil {
				return err
			}
		}

		// Data actually changed: recompute derived state. A pure duplicate
		// batch (toInsert empty) appends no runs/evaluations, since nothing in
		// summaries, budgets or baselines could change.
		if len(toInsert) > 0 {
			ccIDs := mapKeys(ccSet)
			if err := recomputeSlices(ctx, q, minD, maxD, accountIDs, ccIDs); err != nil {
				return err
			}

			run, err := q.CreateAnomalyRun(ctx, dbgen.CreateAnomalyRunParams{
				Kind:     "import",
				ImportID: textVal(imp.ID),
			})
			if err != nil {
				return err
			}
			periods := monthsBetween(minD, maxD)
			if err := evaluateBudgets(ctx, q, ccIDs, periods, run.ID); err != nil {
				return err
			}
			if err := evaluateAnomalies(ctx, q, accountIDs, minD, maxD.AddDate(0, 0, 30), run.ID); err != nil {
				return err
			}
		}

		result = &ImportResult{
			ImportID:          imp.ID,
			RawRowCount:       len(rows),
			InsertedRowCount:  len(toInsert),
			DuplicateRowCount: dupCount,
			MinDate:           minD,
			MaxDate:           maxD,
			Accounts:          accountIDs,
		}
		return nil
	})
	if err != nil {
		var bf *batchFailure
		if errors.As(err, &bf) {
			return nil, bf.ve
		}
		return nil, err
	}
	return result, nil
}

// recordFailedImport persists a failed-batch audit row in its own transaction,
// independent of the rolled-back data transaction.
func (s *Service) recordFailedImport(ctx context.Context, p *auth.Principal, orgID, filename string, rawRows int, ve *ValidationError) {
	_ = s.transact(ctx, func(tx pgx.Tx) error {
		q := dbgen.New(tx)
		_, err := q.CreateImport(ctx, dbgen.CreateImportParams{
			OrgID:        textVal(orgID),
			UserID:       textVal(p.ID),
			Filename:     filename,
			Status:       dbgen.ImportStatusFailed,
			RawRowCount:  int32(rawRows),
			ErrorLine:    pgtype.Int4{Int32: int32(ve.Line), Valid: ve.Line > 0},
			ErrorCode:    pgtype.Text{String: ve.Code, Valid: true},
			ErrorMessage: pgtype.Text{String: truncate(ve.Message, 1000), Valid: true},
		})
		return err
	})
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

func uniqueCodes(n int, get func(int) string) []string {
	set := make(map[string]struct{}, n)
	out := make([]string, 0, n)
	for i := 0; i < n; i++ {
		v := get(i)
		if _, ok := set[v]; !ok {
			set[v] = struct{}{}
			out = append(out, v)
		}
	}
	sort.Strings(out)
	return out
}

func mapKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func monthsBetween(minD, maxD time.Time) []time.Time {
	var out []time.Time
	for m := monthStart(minD); !m.After(monthStart(maxD)); m = m.AddDate(0, 1, 0) {
		out = append(out, m)
	}
	return out
}

func textVal(s string) pgtype.Text {
	return pgtype.Text{String: s, Valid: true}
}
