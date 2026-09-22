package service

import (
	"context"

	"costlens/internal/auth"
)

// VisibilityFor resolves the concrete account and cost-center ID lists the
// principal may see. Admin gets nil (= no filter). Every read path uses this,
// so records, summaries, anomalies and exports share one isolation rule.
func (s *Service) VisibilityFor(ctx context.Context, p *auth.Principal) (Visibility, error) {
	v := Visibility{OrgIDs: p.OrgIDs()}
	if p.IsAdmin() {
		return v, nil
	}
	rows, err := s.pool.Query(ctx,
		`SELECT id, cost_center_id FROM accounts
		 WHERE org_id = ANY($1)`, v.OrgIDs)
	if err != nil {
		return v, err
	}
	defer rows.Close()
	ccSet := map[int64]struct{}{}
	for rows.Next() {
		var accountID, ccID int64
		if err := rows.Scan(&accountID, &ccID); err != nil {
			return v, err
		}
		v.AccountIDs = append(v.AccountIDs, accountID)
		ccSet[ccID] = struct{}{}
	}
	if err := rows.Err(); err != nil {
		return v, err
	}
	for id := range ccSet {
		v.CCIDs = append(v.CCIDs, id)
	}
	return v, nil
}
