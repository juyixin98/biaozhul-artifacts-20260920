package service

import (
	"context"
	"encoding/csv"
	"io"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	"costlens/internal/db"
	"costlens/internal/decimalx"
)

// Visibility holds the scoped id lists derived from the principal.
type Visibility struct {
	OrgIDs     []int64 // nil == all (admin)
	AccountIDs []int64 // nil == all
	CCIDs      []int64 // nil == all
}

type RecordFilter struct {
	From     time.Time
	To       time.Time
	Currency string
}

func (s *Service) ListRecords(ctx context.Context, v Visibility, f RecordFilter,
	limit, offset int32) (*Paged[*BillingRecordDTO], error) {
	params := db.ListBillingRecordsParams{
		UsageDate:      pgDate(f.From),
		UsageDate_2:    pgDate(f.To),
		CurrencyFilter: f.Currency != "",
		Currency:       f.Currency,
		Limit:          limit,
		Offset:         offset,
		AccountIds:     v.AccountIDs,
	}
	rs, err := s.q.ListBillingRecords(ctx, params)
	if err != nil {
		return nil, err
	}
	total, err := s.q.CountBillingRecords(ctx, db.CountBillingRecordsParams{
		UsageDate:      pgDate(f.From),
		UsageDate_2:    pgDate(f.To),
		CurrencyFilter: f.Currency != "",
		Currency:       f.Currency,
		AccountIds:     v.AccountIDs,
	})
	if err != nil {
		return nil, err
	}
	items := make([]*BillingRecordDTO, 0, len(rs))
	for _, r := range rs {
		items = append(items, recordDTO(r))
	}
	return &Paged[*BillingRecordDTO]{Items: items, Limit: limit, Offset: offset, Total: total}, nil
}

// StreamRecordsExport writes CSV incrementally (1000-row pages) so exports do
// not load the whole result set into memory.
func (s *Service) StreamRecordsExport(ctx context.Context, v Visibility,
	f RecordFilter, w io.Writer) error {
	cw := csv.NewWriter(w)
	if err := cw.Write([]string{"account_external_id", "resource_id", "service",
		"date", "currency", "amount"}); err != nil {
		return err
	}
	const page = 1000
	var offset int32
	accountExt := map[int64]string{}
	for {
		params := db.ListBillingRecordsParams{
			UsageDate: pgDate(f.From), UsageDate_2: pgDate(f.To),
			CurrencyFilter: f.Currency != "", Currency: f.Currency,
			Limit: page, Offset: offset, AccountIds: v.AccountIDs,
		}
		rs, err := s.q.ListBillingRecords(ctx, params)
		if err != nil {
			return err
		}
		for _, r := range rs {
			ext, ok := accountExt[r.AccountID]
			if !ok {
				ext = s.accountExternal(ctx, r.AccountID)
				accountExt[r.AccountID] = ext
			}
			if err := cw.Write([]string{
				ext, r.ResourceID, r.Service,
				r.UsageDate.Time.Format("2006-01-02"),
				strings.TrimSpace(r.Currency),
				decimalx.Fixed(r.Amount, 6),
			}); err != nil {
				return err
			}
		}
		cw.Flush()
		if err := cw.Error(); err != nil {
			return err
		}
		if len(rs) < page {
			return nil
		}
		offset += page
	}
}

func (s *Service) accountExternal(ctx context.Context, id int64) string {
	var ext string
	err := s.pool.QueryRow(ctx, `SELECT external_id FROM accounts WHERE id=$1`, id).Scan(&ext)
	if err != nil {
		return ""
	}
	return ext
}

func recordDTO(r db.BillingRecord) *BillingRecordDTO {
	return &BillingRecordDTO{
		ID: r.ID, AccountID: r.AccountID, ResourceID: r.ResourceID,
		Service: r.Service, Date: r.UsageDate.Time.Format("2006-01-02"),
		Currency:    strings.TrimSpace(r.Currency),
		Amount:      decimalx.Fixed(r.Amount, 6),
		ContentHash: r.ContentHash, BatchID: r.BatchID,
	}
}

func (s *Service) ListDaily(ctx context.Context, v Visibility,
	scope string, from, to time.Time) ([]*SummaryDTO, error) {
	rs, err := s.q.ListDailySummaries(ctx, db.ListDailySummariesParams{
		Scope: scope, UsageDate: pgDate(from), UsageDate_2: pgDate(to),
		AccountIds: v.AccountIDs, CcIds: v.CCIDs,
	})
	if err != nil {
		return nil, err
	}
	out := make([]*SummaryDTO, 0, len(rs))
	for _, r := range rs {
		out = append(out, summaryDTO(r.Scope, r.AccountID, r.CostCenterID,
			r.UsageDate.Time, r.Currency, r.TotalAmount, r.RecordCount))
	}
	return out, nil
}

func (s *Service) ListMonthly(ctx context.Context, v Visibility,
	scope string, from, to time.Time) ([]*SummaryDTO, error) {
	// Boundaries are normalized to month starts for the BETWEEN predicate.
	fromM := monthStart(from)
	toM := monthStart(to)
	rs, err := s.q.ListMonthlySummaries(ctx, db.ListMonthlySummariesParams{
		Scope: scope, Month: pgDate(fromM), Month_2: pgDate(toM),
		AccountIds: v.AccountIDs, CcIds: v.CCIDs,
	})
	if err != nil {
		return nil, err
	}
	out := make([]*SummaryDTO, 0, len(rs))
	for _, r := range rs {
		out = append(out, summaryDTO(r.Scope, r.AccountID, r.CostCenterID,
			r.Month.Time, r.Currency, r.TotalAmount, r.RecordCount))
	}
	return out, nil
}

func summaryDTO(scope string, accountID, ccID int64,
	d time.Time, currency string, amount pgtype.Numeric, count int32) *SummaryDTO {
	dto := &SummaryDTO{
		Scope:       scope,
		Date:        d.Format("2006-01-02"),
		Currency:    strings.TrimSpace(currency),
		TotalAmount: amountString(amount),
		RecordCount: count,
	}
	if accountID != 0 {
		dto.AccountID = &accountID
	}
	if ccID != 0 {
		dto.CostCenterID = &ccID
	}
	return dto
}

func (s *Service) ListBatches(ctx context.Context, v Visibility,
	limit, offset int32) ([]*BatchDTO, error) {
	rs, err := s.q.ListBatches(ctx, db.ListBatchesParams{
		OrgIds: v.OrgIDs, Limit: limit, Offset: offset,
	})
	if err != nil {
		return nil, err
	}
	out := make([]*BatchDTO, 0, len(rs))
	for _, b := range rs {
		dto := &BatchDTO{
			ID: b.ID, AccountID: b.AccountID, Filename: b.Filename,
			TotalRows: b.TotalRows, InsertedRows: b.InsertedRows,
			DuplicateRows: b.DuplicateRows, Status: b.Status,
			CreatedAt: b.CreatedAt.Time,
		}
		if b.CreatedBy.Valid {
			v := b.CreatedBy.Int64
			dto.CreatedBy = &v
		}
		out = append(out, dto)
	}
	return out, nil
}

func (s *Service) ListAnomalies(ctx context.Context, v Visibility,
	onlyAnomalous bool) ([]*AnomalyDTO, error) {
	rs, err := s.q.ListAnomalies(ctx, db.ListAnomaliesParams{
		AccountIds:    v.AccountIDs,
		OnlyAnomalous: onlyAnomalous,
	})
	if err != nil {
		return nil, err
	}
	out := make([]*AnomalyDTO, 0, len(rs))
	for _, e := range rs {
		out = append(out, anomalyDTO(e))
	}
	return out, nil
}

func (s *Service) ListAnomalyVersions(ctx context.Context, accountID int64,
	date, currency string) ([]*AnomalyDTO, error) {
	d, err := time.Parse("2006-01-02", date)
	if err != nil {
		return nil, err
	}
	rs, err := s.q.ListAnomalyVersions(ctx, db.ListAnomalyVersionsParams{
		AccountID: accountID, UsageDate: pgDate(d), Currency: currency,
	})
	if err != nil {
		return nil, err
	}
	out := make([]*AnomalyDTO, 0, len(rs))
	for _, e := range rs {
		out = append(out, anomalyDTO(e))
	}
	return out, nil
}

func anomalyDTO(e db.AnomalyEvaluation) *AnomalyDTO {
	dto := &AnomalyDTO{
		ID: e.ID, AccountID: e.AccountID,
		Date:     e.UsageDate.Time.Format("2006-01-02"),
		Currency: strings.TrimSpace(e.Currency), Version: e.Version,
		Status:       e.Status,
		ActualAmount: decimalx.Fixed(e.ActualAmount, 6),
		BaselineDays: e.BaselineDays,
	}
	if e.BaselineMean.Valid {
		s := decimalx.Fixed(e.BaselineMean, 8)
		dto.BaselineMean = &s
	}
	if e.BaselineStd.Valid {
		s := decimalx.Fixed(e.BaselineStd, 8)
		dto.BaselineStd = &s
	}
	if e.ThresholdAmount.Valid {
		s := decimalx.Fixed(e.ThresholdAmount, 8)
		dto.ThresholdAmount = &s
	}
	if e.BaselineStart.Valid {
		s := e.BaselineStart.Time.Format("2006-01-02")
		dto.BaselineStart = &s
	}
	if e.BaselineEnd.Valid {
		s := e.BaselineEnd.Time.Format("2006-01-02")
		dto.BaselineEnd = &s
	}
	return dto
}
