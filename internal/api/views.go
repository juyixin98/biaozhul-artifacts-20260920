package api

import (
	"strconv"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/clearsettle/clearsettle/internal/db"
	"github.com/clearsettle/clearsettle/internal/service"
)

// paymentMasked renders a payment row with the card number masked.
func paymentMasked(p db.Transaction) map[string]any {
	out := map[string]any{
		"id":                 p.ID.String(),
		"merchant_id":        p.MerchantID.String(),
		"status":             p.Status,
		"amount_cents":       p.AmountCents,
		"captured_cents":     p.CapturedCents,
		"fee_cents":          p.FeeCents,
		"refunded_cents":     p.RefundedCents,
		"refunded_fee_cents": p.RefundedFeeCents,
		"settled_cents":      p.SettledCents,
		"currency":           p.Currency,
		"card":               service.MaskCard(p.CardLast4),
		"created_at":         ts(p.CreatedAt),
	}
	if p.AuthExpiresAt.Valid {
		out["auth_expires_at"] = ts(p.AuthExpiresAt)
	}
	if p.CapturedAt.Valid {
		out["captured_at"] = ts(p.CapturedAt)
	}
	if p.SettledAt.Valid {
		out["settled_at"] = ts(p.SettledAt)
	}
	if p.RefundDeadline.Valid {
		out["refund_deadline"] = ts(p.RefundDeadline)
	}
	return out
}

func merchantJSON(m db.Merchant) map[string]any {
	return map[string]any{
		"id":              m.ID.String(),
		"name":            m.Name,
		"fee_bps":         m.FeeBps,
		"fee_fixed_cents": m.FeeFixedCents,
		"active":          m.Active,
		"created_at":      ts(m.CreatedAt),
	}
}

func batchJSON(b db.SettlementBatch) map[string]any {
	return map[string]any{
		"id":           b.ID.String(),
		"merchant_id":  b.MerchantID.String(),
		"batch_date":   b.BatchDate.Format("2006-01-02"),
		"status":       b.Status,
		"total_cents":  b.TotalCents,
		"fee_cents":    b.FeeCents,
		"net_cents":    b.NetCents,
		"created_at":   ts(b.CreatedAt),
		"completed_at": tsOrNull(b.CompletedAt),
	}
}

func reconRunJSON(run db.ReconciliationRun) map[string]any {
	var batchID any
	if run.BatchID != nil {
		batchID = run.BatchID.String()
	}
	return map[string]any{
		"id":                  run.ID.String(),
		"merchant_id":         run.MerchantID.String(),
		"run_date":            run.RunDate.Format("2006-01-02"),
		"status":              run.Status,
		"batch_id":            batchID,
		"captured_cents":      run.CapturedCents,
		"refunded_cents":      run.RefundedCents,
		"fees_cents":          run.FeesCents,
		"settled_cents":       run.SettledCents,
		"gateway_cash_cents":  run.GatewayCashCents,
		"payable_cents":       run.PayableCents,
		"expected_cash_cents": run.ExpectedCashCents,
		"diff_cents":          run.DiffCents,
		"discrepancies_count": run.DiscrepanciesCount,
		"completed_at":        tsOrNull(run.CompletedAt),
	}
}

func reconJSON(res service.ReconResult) map[string]any {
	out := reconRunJSON(res.Run)
	discs := make([]map[string]any, 0, len(res.Discrepancies))
	for _, d := range res.Discrepancies {
		discs = append(discs, map[string]any{
			"id":             d.ID.String(),
			"kind":           d.Kind,
			"tx_id":          uuidPtrString(d.TxID),
			"expected_cents": d.ExpectedCents,
			"actual_cents":   d.ActualCents,
			"diff_cents":     d.DiffCents,
			"detail":         d.Detail,
		})
	}
	out["discrepancies"] = discs
	out["already_existed"] = res.AlreadyExisted
	return out
}

func uuidPtrString(p *uuid.UUID) any {
	if p == nil {
		return nil
	}
	return p.String()
}

func ts(t pgtype.Timestamptz) string {
	if !t.Valid {
		return ""
	}
	return t.Time.UTC().Format(time.RFC3339)
}

func tsOrNull(t pgtype.Timestamptz) any {
	if !t.Valid {
		return nil
	}
	return t.Time.UTC().Format(time.RFC3339)
}

func atoiDefault(s string, def int) int {
	if s == "" {
		return def
	}
	v, err := strconv.Atoi(s)
	if err != nil {
		return def
	}
	return v
}
