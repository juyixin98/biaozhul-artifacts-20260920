package services

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"

	"communitygov/internal/database"
	"communitygov/internal/database/sqlcgen"
)

type UserService struct{ store *database.Store }

func NewUserService(s *database.Store) *UserService { return &UserService{store: s} }

func (s *UserService) Register(ctx context.Context, email, name, password, role string) (sqlcgen.User, error) {
	if email == "" || password == "" {
		return sqlcgen.User{}, E(ErrValidation, "email and password required")
	}
	switch role {
	case "", "member":
		role = "member"
	case "reviewer", "admin":
	default:
		return sqlcgen.User{}, E(ErrValidation, "role must be one of admin/reviewer/member")
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	if err != nil {
		return sqlcgen.User{}, err
	}
	u, err := s.store.CreateUser(ctx, sqlcgen.CreateUserParams{
		Email: email, DisplayName: name, Role: role, PasswordHash: string(hash),
	})
	if err != nil {
		if isUniqueViolation(err) {
			return sqlcgen.User{}, E(ErrConflict, "email already registered")
		}
		return sqlcgen.User{}, err
	}
	return u, nil
}

func (s *UserService) Login(ctx context.Context, email, password string) (sqlcgen.User, error) {
	u, err := s.store.GetUserByEmail(ctx, email)
	if errors.Is(err, pgx.ErrNoRows) {
		return sqlcgen.User{}, E(ErrUnauthorized, "invalid credentials")
	} else if err != nil {
		return sqlcgen.User{}, err
	}
	if err := bcrypt.CompareHashAndPassword([]byte(u.PasswordHash), []byte(password)); err != nil {
		return sqlcgen.User{}, E(ErrUnauthorized, "invalid credentials")
	}
	return u, nil
}

func (s *UserService) GetByID(ctx context.Context, id int64) (sqlcgen.User, error) {
	u, err := s.store.GetUserByID(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return sqlcgen.User{}, E(ErrNotFound, "user")
	}
	return u, err
}

func (s *UserService) CreateCommunity(ctx context.Context, name string, adminID int64) (sqlcgen.Community, error) {
	if name == "" {
		return sqlcgen.Community{}, E(ErrValidation, "name required")
	}
	return s.store.CreateCommunity(ctx, sqlcgen.CreateCommunityParams{Name: name, CreatedBy: adminID})
}

func (s *UserService) ListCommunities(ctx context.Context) ([]sqlcgen.Community, error) {
	return s.store.ListCommunities(ctx)
}

func (s *UserService) AddReviewer(ctx context.Context, communityID, userID int64) error {
	if _, err := s.store.GetUserByID(ctx, userID); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "user")
		}
		return err
	}
	return s.store.AddReviewer(ctx,
		sqlcgen.AddReviewerParams{CommunityID: communityID, UserID: userID})
}

func (s *UserService) IsReviewer(ctx context.Context, communityID, userID int64) bool {
	ok, err := s.store.IsReviewer(ctx,
		sqlcgen.IsReviewerParams{CommunityID: communityID, UserID: userID})
	return err == nil && ok
}
