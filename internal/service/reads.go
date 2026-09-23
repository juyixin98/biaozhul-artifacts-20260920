package service

import (
	"context"
	"encoding/csv"
	"io"
	"time"

	"costlens/internal/auth"
	"costlens/internal/db/dbgen"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

// Filters are optional query filters applied to scoped list/export endpoints.
type Filters struct {
	AccountID    string
	CostCenterID string
	Currency     string
	DateEq       time.Time
	DateFrom     time.Time
	DateTo       time.Time
	Period       time.Time // exact month start, for monthly views
	Limit        int32
	Offset       int32
}

func (f Filters) dateEq() pgtype.Date {
	if f.DateEq.IsZero() {
		return pgtype.Date{Valid: false}
	}
	return pgtype.Date{Time: truncDate(f.DateEq), Valid: true}
}

func (f Filters) dateFrom() pgtype.Date {
	if f.DateFrom.IsZero() {
		return pgtype.Date{Valid: false}
	}
	return pgtype.Date{Time: truncDate(f.DateFrom), Valid: true}
}

func (f Filters) dateTo() pgtype.Date {
	if f.DateTo.IsZero() {
		return pgtype.Date{Valid: false}
	}
	return pgtype.Date{Time: truncDate(f.DateTo), Valid: true}
}

func (f Filters) period() pgtype.Date {
	if f.Period.IsZero() {
		return pgtype.Date{Valid: false}
	}
	// Monthly scoped queries take from/to; treat exact period as a one-month
	// range when set via this helper in handlers if needed.
	return pgtype.Date{Time: monthStart(f.Period), Valid: true}
}

// ReadTx runs fn with a read-only transaction and scoped queries.
func (s *Service) readTx(ctx context.Context, fn func(pgx.Tx) error) error {
	return s.transact(ctx, fn)
}

// ---- Catalog ----

func (s *Service) ListOrganizations(ctx context.Context, p *auth.Principal) ([]dbgen.Organization, error) {
	var out []dbgen.Organization
	err := s.readTx(ctx, func(tx pgx.Tx) error {
		q := dbgen.New(tx)
		if p.Role == auth.RoleAdmin {
			orgs, err := q.ListOrganizations(ctx)
			if err != nil {
				return err
			}
			out = orgs
			return nil
		}
		orgs, err := q.ListOrganizations(ctx)
		if err != nil {
			return err
		}
		for _, o := range orgs {
			if p.CanAccessOrg(o.ID) {
				out = append(out, o)
			}
		}
		return nil
	})
	return out, err
}

func (s *Service) ListCostCenters(ctx context.Context, p *auth.Principal, orgIDs []string) ([]dbgen.CostCenter, error) {
	orgIDs = filterScope(p, orgIDs)
	if len(orgIDs) == 0 {
		return nil, nil
	}
	var out []dbgen.CostCenter
	err := s.readTx(ctx, func(tx pgx.Tx) error {
		var err error
		out, err = dbgen.New(tx).ListCostCentersScoped(ctx, orgIDs)
		return err
	})
	return out, err
}

func (s *Service) ListAccounts(ctx context.Context, p *auth.Principal, orgIDs []string) ([]dbgen.Account, error) {
	orgIDs = filterScope(p, orgIDs)
	if len(orgIDs) == 0 {
		return nil, nil
	}
	var out []dbgen.Account
	err := s.readTx(ctx, func(tx pgx.Tx) error {
		var err error
		out, err = dbgen.New(tx).ListAccountsScoped(ctx, orgIDs)
		return err
	})
	return out, err
}

func filterScope(p *auth.Principal, requested []string) []string {
	allowed := map[string]bool{}
	for _, o := range p.OrgIDs { // populated for admins at authentication time
		allowed[o] = true
	}
	if len(requested) == 0 {
		return p.ScopeOrgIDs()
	}
	out := make([]string, 0, len(requested))
	for _, o := range requested {
		if allowed[o] {
			out = append(out, o)
		}
	}
	return out
}

// ---- Costs ----

func (s *Service) ListCosts(ctx context.Context, p *auth.Principal, orgIDs []string, f Filters) ([]dbgen.Cost, int64, error) {
	orgIDs = filterScope(p, orgIDs)
	if len(orgIDs) == 0 {
		return nil, 0, nil
	}
	lim := f.Limit
	if lim == 0 || lim > 1000 {
		lim = 1000
	}
	var rows []dbgen.Cost
	var total int64
	err := s.readTx(ctx, func(tx pgx.Tx) error {
		q := dbgen.New(tx)
		args := func(limV, offV int32) dbgen.ListCostsScopedParams {
			return dbgen.ListCostsScopedParams{
				Column1: orgIDs, Limit: limV, Offset: offV,
				AccountID: auth.TextNull(f.AccountID),
				DateEq:    f.dateEq(), DateFrom: f.dateFrom(), DateTo: f.dateTo(),
				Currency: auth.TextNull(f.Currency),
			}
		}
		var err error
		rows, err = q.ListCostsScoped(ctx, args(lim, f.Offset))
		if err != nil {
			return err
		}
		total, err = q.CountCostsScoped(ctx, dbgen.CountCostsScopedParams{
			Column1: orgIDs, AccountID: auth.TextNull(f.AccountID),
			DateEq: f.dateEq(), DateFrom: f.dateFrom(), DateTo: f.dateTo(),
			Currency: auth.TextNull(f.Currency),
		})
		return err
	})
	return rows, total, err
}

// ExportCosts streams scoped cost rows as CSV to w. It runs in one read tx so a
// concurrent rebuild/import cannot interleave a partial view (the tx snapshot
// is consistent).
func (s *Service) ExportCosts(ctx context.Context, p *auth.Principal, orgIDs []string, f Filters, w io.Writer) error {
	orgIDs = filterScope(p, orgIDs)
	if len(orgIDs) == 0 {
		return nil
	}
	return s.readTx(ctx, func(tx pgx.Tx) error {
		q := dbgen.New(tx)
		rows, err := q.StreamCostsScoped(ctx, dbgen.StreamCostsScopedParams{
			Column1: orgIDs, AccountID: auth.TextNull(f.AccountID),
			DateEq: f.dateEq(), DateFrom: f.dateFrom(), DateTo: f.dateTo(),
			Currency: auth.TextNull(f.Currency),
		})
		if err != nil {
			return err
		}
		accts, err := q.ListAccountsScoped(ctx, orgIDs)
		if err != nil {
			return err
		}
		acctCode := map[string]string{}
		for _, a := range accts {
			acctCode[a.ID] = a.Code
		}
		resIDset := map[string]bool{}
		for _, r := range rows {
			resIDset[r.ResourceID] = true
		}
		resIDs := make([]string, 0, len(resIDset))
		for id := range resIDset {
			resIDs = append(resIDs, id)
		}
		resources, err := q.GetResourcesByIDs(ctx, resIDs)
		if err != nil {
			return err
		}
		resCode := map[string]string{}
		for _, rr := range resources {
			resCode[rr.ID] = rr.Code
		}

		cw := csv.NewWriter(w)
		if err := cw.Write([]string{"account_code", "resource_code", "service", "cost_date", "currency", "amount"}); err != nil {
			return err
		}
		for _, r := range rows {
			if err := cw.Write([]string{
				acctCode[r.AccountID], resCode[r.ResourceID], r.Service,
				dateStr(r.CostDate), r.Currency, r.Amount.String(),
			}); err != nil {
				return err
			}
		}
		cw.Flush()
		return cw.Error()
	})
}
