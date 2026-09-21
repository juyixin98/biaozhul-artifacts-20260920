// Package admin handles merchant and user administration, including operator
// API key issuance. Plaintext keys are returned exactly once (on create and
// rotate); only a sha256 hash plus a non-reversible prefix is stored.
package admin

import (
	"context"
	"errors"
	"net/mail"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/clearsettle/clearsettle/internal/audit"
	"github.com/clearsettle/clearsettle/internal/auth"
	"github.com/clearsettle/clearsettle/internal/ledger"
	"github.com/clearsettle/clearsettle/internal/money"
	"github.com/clearsettle/clearsettle/internal/store"
)

var (
	ErrNotFound     = errors.New("not found")
	ErrInvalidInput = errors.New("invalid input")
	ErrEmailTaken   = errors.New("email already registered")
	ErrBadFee       = errors.New("invalid fee schedule")
)

type Service struct {
	pool *pgxpool.Pool
	q    *store.Queries
}

func New(pool *pgxpool.Pool) *Service { return &Service{pool: pool, q: store.New(pool)} }

type Actor struct {
	UserID *uuid.UUID
	Role   string
	IP     string
}

type CreateMerchantInput struct {
	Name     string
	FeeBps   int32
	FeeFixed int64
}

type MerchantResult struct {
	Merchant store.Merchant
	APIKey   string // plaintext, returned once
}

func (s *Service) CreateMerchant(ctx context.Context, in CreateMerchantInput, actor Actor) (MerchantResult, error) {
	in.Name = strings.TrimSpace(in.Name)
	if in.Name == "" {
		return MerchantResult{}, ErrInvalidInput
	}
	if in.FeeBps == 0 && in.FeeFixed == 0 {
		in.FeeBps, in.FeeFixed = 290, 30
	}
	if err := money.ValidFeeSchedule(int(in.FeeBps), money.Cents(in.FeeFixed)); err != nil {
		return MerchantResult{}, err
	}
	plaintext, keyHash, prefix, err := auth.GenerateAPIKey()
	if err != nil {
		return MerchantResult{}, err
	}

	var res MerchantResult
	err = pgx.BeginTxFunc(ctx, s.pool, pgx.TxOptions{}, func(tx pgx.Tx) error {
		q := store.New(tx)
		m, err := q.CreateMerchant(ctx, store.CreateMerchantParams{
			Name:         in.Name,
			FeeBps:       in.FeeBps,
			FeeFixed:     in.FeeFixed,
			ApiKeyHash:   nullText(keyHash),
			ApiKeyPrefix: nullText(prefix),
		})
		if err != nil {
			return err
		}
		if err := ledger.EnsureMerchantAccount(ctx, q, m.ID); err != nil {
			return err
		}
		if err := audit.Write(ctx, q, audit.Entry{
			ActorUser: actor.UserID, ActorRole: actor.Role, MerchantID: &m.ID,
			Action: "merchant.create", TargetType: "merchant", TargetID: m.ID.String(),
			Masked: true,
			Detail: map[string]any{
				"name": in.Name, "fee_bps": in.FeeBps, "fee_fixed": in.FeeFixed,
				"api_key_prefix": prefix,
			},
			IP: actor.IP,
		}); err != nil {
			return err
		}
		res = MerchantResult{Merchant: m, APIKey: plaintext}
		return nil
	})
	return res, err
}

func (s *Service) RotateAPIKey(ctx context.Context, merchantID uuid.UUID, actor Actor) (string, error) {
	plaintext, keyHash, prefix, err := auth.GenerateAPIKey()
	if err != nil {
		return "", err
	}
	err = pgx.BeginTxFunc(ctx, s.pool, pgx.TxOptions{}, func(tx pgx.Tx) error {
		q := store.New(tx)
		m, err := q.RotateMerchantAPIKey(ctx, store.RotateMerchantAPIKeyParams{
			ID: merchantID, ApiKeyHash: nullText(keyHash), ApiKeyPrefix: nullText(prefix),
		})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		return audit.Write(ctx, q, audit.Entry{
			ActorUser: actor.UserID, ActorRole: actor.Role, MerchantID: &merchantID,
			Action: "merchant.rotate_api_key", TargetType: "merchant", TargetID: m.ID.String(),
			Masked: true, Detail: map[string]any{"api_key_prefix": prefix}, IP: actor.IP,
		})
	})
	if err != nil {
		return "", err
	}
	return plaintext, nil
}

func (s *Service) SetMerchantStatus(ctx context.Context, merchantID uuid.UUID, status string, actor Actor) error {
	if status != "active" && status != "suspended" {
		return ErrInvalidInput
	}
	return pgx.BeginTxFunc(ctx, s.pool, pgx.TxOptions{}, func(tx pgx.Tx) error {
		q := store.New(tx)
		_, err := q.SetMerchantStatus(ctx, store.SetMerchantStatusParams{ID: merchantID, Status: status})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		return audit.Write(ctx, q, audit.Entry{
			ActorUser: actor.UserID, ActorRole: actor.Role, MerchantID: &merchantID,
			Action: "merchant.set_status", TargetType: "merchant", TargetID: merchantID.String(),
			Detail: map[string]any{"status": status}, IP: actor.IP,
		})
	})
}

func (s *Service) ListMerchants(ctx context.Context) ([]store.Merchant, error) {
	return s.q.ListMerchants(ctx)
}

type CreateUserInput struct {
	Email      string
	Password   string
	Role       string
	MerchantID *uuid.UUID
}

func (s *Service) CreateUser(ctx context.Context, in CreateUserInput, actor Actor) (store.User, error) {
	email := strings.ToLower(strings.TrimSpace(in.Email))
	if _, err := mail.ParseAddress(email); err != nil || len(in.Password) < 10 {
		return store.User{}, ErrInvalidInput
	}
	switch in.Role {
	case auth.RoleAdmin:
		in.MerchantID = nil
	case auth.RoleOperator, auth.RoleAuditor:
		if in.MerchantID == nil {
			return store.User{}, ErrInvalidInput
		}
	default:
		return store.User{}, ErrInvalidInput
	}
	hash, err := auth.HashPassword(in.Password)
	if err != nil {
		return store.User{}, err
	}
	var u store.User
	err = pgx.BeginTxFunc(ctx, s.pool, pgx.TxOptions{}, func(tx pgx.Tx) error {
		q := store.New(tx)
		u, err = q.CreateUser(ctx, store.CreateUserParams{
			Email: email, PasswordHash: hash, Role: in.Role, MerchantID: in.MerchantID,
		})
		if isUnique(err) {
			return ErrEmailTaken
		}
		if err != nil {
			return err
		}
		return audit.Write(ctx, q, audit.Entry{
			ActorUser: actor.UserID, ActorRole: actor.Role, MerchantID: in.MerchantID,
			Action: "user.create", TargetType: "user", TargetID: u.ID.String(),
			Masked: true, Detail: map[string]any{"email": auth.MaskEmail(email), "role": in.Role},
			IP: actor.IP,
		})
	})
	return u, err
}

func (s *Service) AuthenticateUser(ctx context.Context, email, password, ip string) (store.User, error) {
	email = strings.ToLower(strings.TrimSpace(email))
	u, err := s.q.GetUserByEmail(ctx, email)
	if errors.Is(err, pgx.ErrNoRows) {
		return store.User{}, ErrNotFound
	}
	if err != nil {
		return store.User{}, err
	}
	if u.Status != "active" || !auth.CheckPassword(u.PasswordHash, password) {
		return store.User{}, ErrNotFound
	}
	return u, nil
}

func (s *Service) AuthenticateAPIKey(ctx context.Context, key string) (store.Merchant, error) {
	if strings.TrimSpace(key) == "" {
		return store.Merchant{}, ErrNotFound
	}
	return s.q.GetMerchantByAPIKeyHash(ctx, nullText(auth.HashAPIKey(key)))
}

func isUnique(err error) bool {
	return err != nil && strings.Contains(err.Error(), "23505")
}

func nullText(s string) pgtype.Text {
	if s == "" {
		return pgtype.Text{}
	}
	return pgtype.Text{String: s, Valid: true}
}
