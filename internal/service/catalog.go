package service

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"time"

	"costlens/internal/db"
	"costlens/internal/decimalx"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

var ErrNotFound = errors.New("not found")
var ErrConflict = errors.New("conflict")

func pgDate(t time.Time) pgtype.Date {
	return pgtype.Date{Time: time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC), Valid: true}
}

func pgInt8(v int64) pgtype.Int8 { return pgtype.Int8{Int64: v, Valid: true} }

// ---------- Organizations ----------

type CreateOrgInput struct {
	ExternalID string `json:"external_id"`
	Name       string `json:"name"`
}

func (s *Service) CreateOrg(ctx context.Context, in CreateOrgInput) (*OrgDTO, error) {
	o, err := s.q.CreateOrg(ctx, db.CreateOrgParams{ExternalID: in.ExternalID, Name: in.Name})
	if err != nil {
		return nil, err
	}
	return &OrgDTO{ID: o.ID, ExternalID: o.ExternalID, Name: o.Name, CreatedAt: o.CreatedAt.Time}, nil
}

func (s *Service) ListOrgs(ctx context.Context) ([]*OrgDTO, error) {
	os, err := s.q.ListOrgs(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]*OrgDTO, 0, len(os))
	for _, o := range os {
		out = append(out, &OrgDTO{ID: o.ID, ExternalID: o.ExternalID, Name: o.Name, CreatedAt: o.CreatedAt.Time})
	}
	return out, nil
}

// ---------- Cost centers ----------

type CreateCostCenterInput struct {
	OrgExternalID string `json:"org_external_id"`
	Code          string `json:"code"`
	Name          string `json:"name"`
}

func (s *Service) CreateCostCenter(ctx context.Context, in CreateCostCenterInput) (*CostCenterDTO, error) {
	o, err := s.q.GetOrgByExternalID(ctx, in.OrgExternalID)
	if err != nil {
		return nil, ErrNotFound
	}
	cc, err := s.q.CreateCostCenter(ctx, db.CreateCostCenterParams{OrgID: o.ID, Code: in.Code, Name: in.Name})
	if err != nil {
		return nil, err
	}
	return &CostCenterDTO{ID: cc.ID, OrgID: cc.OrgID, Code: cc.Code, Name: cc.Name}, nil
}

// ---------- Accounts ----------

type CreateAccountInput struct {
	OrgExternalID  string `json:"org_external_id"`
	CostCenterCode string `json:"cost_center_code"`
	ExternalID     string `json:"external_id"`
	Name           string `json:"name"`
}

func (s *Service) CreateAccount(ctx context.Context, in CreateAccountInput) (*AccountDTO, error) {
	o, err := s.q.GetOrgByExternalID(ctx, in.OrgExternalID)
	if err != nil {
		return nil, ErrNotFound
	}
	cc, err := s.q.GetCostCenterByCode(ctx, db.GetCostCenterByCodeParams{OrgID: o.ID, Code: in.CostCenterCode})
	if err != nil {
		return nil, ErrNotFound
	}
	a, err := s.q.CreateAccount(ctx, db.CreateAccountParams{
		OrgID: o.ID, CostCenterID: cc.ID, ExternalID: in.ExternalID, Name: in.Name,
	})
	if err != nil {
		return nil, err
	}
	return &AccountDTO{
		ID: a.ID, OrgID: a.OrgID, CostCenterID: a.CostCenterID,
		ExternalID: a.ExternalID, Name: a.Name,
	}, nil
}

// GetAccountForImport returns the account plus its org; existence is enforced.
func (s *Service) getAccountByExternal(ctx context.Context, ext string) (db.Account, error) {
	a, err := s.q.GetAccountByExternalID(ctx, ext)
	if errors.Is(err, pgx.ErrNoRows) {
		return db.Account{}, ErrNotFound
	}
	return a, err
}

// AccountForImport returns account info for authorization checks.
func (s *Service) AccountForImport(ctx context.Context, ext string) (*AccountDTO, error) {
	a, err := s.getAccountByExternal(ctx, ext)
	if err != nil {
		return nil, err
	}
	return &AccountDTO{
		ID: a.ID, OrgID: a.OrgID, CostCenterID: a.CostCenterID,
		ExternalID: a.ExternalID, Name: a.Name,
	}, nil
}

func (s *Service) ListCostCenters(ctx context.Context, orgIDs []int64) ([]*CostCenterDTO, error) {
	cs, err := s.q.ListCostCenters(ctx, orgIDs)
	if err != nil {
		return nil, err
	}
	out := make([]*CostCenterDTO, 0, len(cs))
	for _, c := range cs {
		out = append(out, &CostCenterDTO{ID: c.ID, OrgID: c.OrgID, Code: c.Code, Name: c.Name})
	}
	return out, nil
}

func (s *Service) ListAccounts(ctx context.Context, orgIDs []int64) ([]*AccountDTO, error) {
	as, err := s.q.ListAccounts(ctx, orgIDs)
	if err != nil {
		return nil, err
	}
	out := make([]*AccountDTO, 0, len(as))
	for _, a := range as {
		out = append(out, &AccountDTO{
			ID: a.ID, OrgID: a.OrgID, CostCenterID: a.CostCenterID,
			ExternalID: a.ExternalID, Name: a.Name,
		})
	}
	return out, nil
}

// ---------- Users / grants ----------

type CreateUserInput struct {
	Username string `json:"username"`
	Role     string `json:"role"`
}

func (s *Service) CreateUser(ctx context.Context, in CreateUserInput) (username, token string, err error) {
	tok, err := randomToken()
	if err != nil {
		return "", "", err
	}
	u, err := s.q.CreateUser(ctx, db.CreateUserParams{Username: in.Username, Role: in.Role, ApiToken: tok})
	if err != nil {
		return "", "", err
	}
	return u.Username, u.ApiToken, nil
}

func (s *Service) Grant(ctx context.Context, username, orgExternalID string) error {
	var u db.User
	err := s.pool.QueryRow(ctx, `SELECT id, username, role, api_token, created_at FROM users WHERE username=$1`, username).
		Scan(&u.ID, &u.Username, &u.Role, &u.ApiToken, &u.CreatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	}
	if err != nil {
		return err
	}
	o, err := s.q.GetOrgByExternalID(ctx, orgExternalID)
	if err != nil {
		return ErrNotFound
	}
	return s.q.GrantOrg(ctx, db.GrantOrgParams{UserID: u.ID, OrgID: o.ID})
}

func randomToken() (string, error) {
	b := make([]byte, 24)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return "clt_" + hex.EncodeToString(b), nil
}

// amountString renders a pgtype.Numeric as a plain decimal string.
func amountString(n pgtype.Numeric) string {
	return decimalx.Fixed(n, 6)
}
