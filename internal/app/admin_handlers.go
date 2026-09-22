package app

import (
	"errors"
	"fmt"
	"net/http"
	"strconv"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"community-governance/internal/database"
	"community-governance/internal/httpx"
)

// createCommunity bootstraps a community and returns its admin user + token.
// Any authenticated caller may create a new community and becomes its admin.
func (a *App) createCommunity(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Name      string `json:"name"`
		AdminName string `json:"admin_name"`
	}
	if err := httpx.Decode(r, &req); err != nil || req.Name == "" || req.AdminName == "" {
		badRequest(w, "name and admin_name are required")
		return
	}
	tok := "tok_" + uuid.NewString()
	c, err := a.Q.CreateCommunity(r.Context(), req.Name)
	if err != nil {
		serverError(w, err)
		return
	}
	u, err := a.Q.CreateUser(r.Context(), database.CreateUserParams{
		CommunityID: c.ID, Username: req.AdminName, Role: "admin", Token: tok,
	})
	if err != nil {
		serverError(w, err)
		return
	}
	httpx.JSON(w, http.StatusCreated, map[string]any{
		"community": c, "admin": u, "admin_token": tok,
	})
}

func (a *App) createUser(w http.ResponseWriter, r *http.Request) {
	cid, ok := a.loadCommunityOr404(w, r)
	if !ok {
		return
	}
	u := currentUser(r)
	if !requireAdmin(w, u) {
		return
	}
	var req struct {
		Username string `json:"username"`
		Role     string `json:"role"`
	}
	if err := httpx.Decode(r, &req); err != nil || req.Username == "" {
		badRequest(w, "username required")
		return
	}
	if req.Role == "" {
		req.Role = "member"
	}
	if req.Role != "admin" && req.Role != "moderator" && req.Role != "member" {
		badRequest(w, "role must be admin, moderator or member")
		return
	}
	tok := "tok_" + uuid.NewString()
	nu, err := a.Q.CreateUser(r.Context(), database.CreateUserParams{
		CommunityID: cid, Username: req.Username, Role: req.Role, Token: tok,
	})
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			conflict(w, "username already exists")
			return
		}
		serverError(w, err)
		return
	}
	httpx.JSON(w, http.StatusCreated, map[string]any{"user": nu, "token": tok})
}

func (a *App) listUsers(w http.ResponseWriter, r *http.Request) {
	cid, ok := a.loadCommunityOr404(w, r)
	if !ok {
		return
	}
	u := currentUser(r)
	if !requireAdmin(w, u) {
		return
	}
	users, err := a.Q.ListUsers(r.Context(), cid)
	if err != nil {
		serverError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"users": users})
}

func (a *App) createTier(w http.ResponseWriter, r *http.Request) {
	cid, ok := a.loadCommunityOr404(w, r)
	if !ok {
		return
	}
	u := currentUser(r)
	if !requireAdmin(w, u) {
		return
	}
	var req struct {
		Level        int32  `json:"level"`
		Name         string `json:"name"`
		PriceCents   int64  `json:"price_cents"`
		DurationDays int32  `json:"duration_days"`
	}
	if err := httpx.Decode(r, &req); err != nil {
		badRequest(w, err.Error())
		return
	}
	if req.Level < 1 || req.Level > 10 || req.Name == "" ||
		req.PriceCents < 0 || req.DurationDays <= 0 {
		badRequest(w, "level 1..10, name, non-negative price_cents, positive duration_days required")
		return
	}
	t, err := a.Q.CreateTier(r.Context(), database.CreateTierParams{
		CommunityID: cid, Level: req.Level, Name: req.Name,
		PriceCents: req.PriceCents, DurationDays: req.DurationDays,
	})
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) {
			if pgErr.Code == "23505" {
				conflict(w, "tier level already exists")
				return
			}
			if pgErr.Code == "check_violation" || pgErr.ConstraintName == "trg_tier_cap" {
				conflict(w, "a community may have at most 10 tiers")
				return
			}
		}
		serverError(w, err)
		return
	}
	httpx.JSON(w, http.StatusCreated, t)
}

func (a *App) listTiers(w http.ResponseWriter, r *http.Request) {
	cid, ok := a.loadCommunityOr404(w, r)
	if !ok {
		return
	}
	tiers, err := a.Q.ListTiers(r.Context(), cid)
	if err != nil {
		serverError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"tiers": tiers})
}

func (a *App) setTierActive(w http.ResponseWriter, r *http.Request) {
	cid, ok := a.loadCommunityOr404(w, r)
	if !ok {
		return
	}
	u := currentUser(r)
	if !requireAdmin(w, u) {
		return
	}
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	var req struct {
		IsActive bool `json:"is_active"`
	}
	if err := httpx.Decode(r, &req); err != nil {
		badRequest(w, err.Error())
		return
	}
	if _, err := a.Q.GetTier(r.Context(), database.GetTierParams{ID: id, CommunityID: cid}); err != nil {
		writeErr(w, err)
		return
	}
	if err := a.Q.SetTierActive(r.Context(), database.SetTierActiveParams{
		ID: id, CommunityID: cid, IsActive: req.IsActive,
	}); err != nil {
		serverError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// registerPayment is admin-only: the admin confirms that an offline payment was
// received locally, records it (idempotent on request_id) and extends the
// member's subscription atomically.
func (a *App) registerPayment(w http.ResponseWriter, r *http.Request) {
	cid, ok := a.loadCommunityOr404(w, r)
	if !ok {
		return
	}
	caller := currentUser(r)
	if !requireAdmin(w, caller) {
		return
	}
	var req struct {
		RequestID   string `json:"request_id"`
		UserID      int64  `json:"user_id"`
		TierID      int64  `json:"tier_id"`
		AmountCents int64  `json:"amount_cents"`
		Days        int32  `json:"days"`
	}
	if err := httpx.Decode(r, &req); err != nil {
		badRequest(w, err.Error())
		return
	}
	if req.RequestID == "" || req.UserID <= 0 || req.TierID <= 0 ||
		req.AmountCents < 0 || req.Days <= 0 {
		badRequest(w, "request_id, user_id, tier_id, non-negative amount_cents, positive days required")
		return
	}

	var sub database.Subscription
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		// Idempotency: same request id must mean the same payload.
		existing, err := q.GetPaymentByRequest(r.Context(),
			database.GetPaymentByRequestParams{CommunityID: cid, RequestID: req.RequestID})
		if err == nil {
			if existing.UserID != req.UserID || existing.TierID != req.TierID ||
				existing.AmountCents != req.AmountCents || existing.Days != req.Days {
				return errConflict
			}
			// Identical replay -> load current subscription and return it.
			sub, err = q.GetSubscription(r.Context(),
				database.GetSubscriptionParams{CommunityID: cid, UserID: req.UserID})
			if err != nil {
				return err
			}
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}

		// Validate target user and tier belong to this community.
		if _, err := q.GetUser(r.Context(), database.GetUserParams{
			ID: req.UserID, CommunityID: cid,
		}); err != nil {
			return fmt.Errorf("target user: %w", errBadRequest)
		}
		tier, err := q.GetTier(r.Context(), database.GetTierParams{ID: req.TierID, CommunityID: cid})
		if err != nil {
			return fmt.Errorf("tier: %w", errBadRequest)
		}
		if !tier.IsActive {
			return fmt.Errorf("tier is not active: %w", errBadRequest)
		}

		// Single atomic upsert: concurrent first-time payments serialize on the
		// unique index and become one insert + one renewal, so no duration is
		// lost and no duplicate-key error escapes.
		sub, err = q.UpsertSubscription(r.Context(), database.UpsertSubscriptionParams{
			CommunityID: cid, UserID: req.UserID, TierID: req.TierID, Days: req.Days,
		})
		if err != nil {
			return err
		}
		if _, err := q.CreatePayment(r.Context(), database.CreatePaymentParams{
			CommunityID: cid, RequestID: req.RequestID, UserID: req.UserID,
			TierID: req.TierID, AmountCents: req.AmountCents, Days: req.Days,
		}); err != nil {
			return err
		}
		return nil
	})
	if txErr != nil {
		var pgErr *pgconn.PgError
		if errors.As(txErr, &pgErr) && pgErr.Code == "23505" {
			conflict(w, "request_id already used (concurrent replay with a different payload)")
			return
		}
		writeErr(w, txErr)
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{
		"subscription": sub,
		"replayed":     false,
	})
}

func (a *App) mySubscription(w http.ResponseWriter, r *http.Request) {
	cid, ok := a.loadCommunityOr404(w, r)
	if !ok {
		return
	}
	u := currentUser(r)
	sub, err := a.Q.GetSubscription(r.Context(),
		database.GetSubscriptionParams{CommunityID: cid, UserID: u.ID})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			httpx.JSON(w, http.StatusOK, map[string]any{"subscription": nil})
			return
		}
		serverError(w, err)
		return
	}
	level, levelErr := a.Q.GetEffectiveLevel(r.Context(), database.GetEffectiveLevelParams{
		CommunityID: cid, UserID: u.ID,
	})
	httpx.JSON(w, http.StatusOK, map[string]any{
		"subscription":    sub,
		"effective_level": level,
		"access":          levelErr == nil,
	})
}

// cancelMySubscription lets a member cancel themselves; an admin may also
// cancel anyone's using ?user_id=. Access is removed immediately.
func (a *App) cancelMySubscription(w http.ResponseWriter, r *http.Request) {
	cid, ok := a.loadCommunityOr404(w, r)
	if !ok {
		return
	}
	u := currentUser(r)
	target := u.ID
	if raw := r.URL.Query().Get("user_id"); raw != "" {
		if !u.isAdmin() {
			forbidden(w, "only admins may cancel another member's subscription")
			return
		}
		id, err := strconv.ParseInt(raw, 10, 64)
		if err != nil || id <= 0 {
			badRequest(w, "invalid user_id")
			return
		}
		target = id
	}
	if err := a.Q.CancelSubscription(r.Context(),
		database.CancelSubscriptionParams{CommunityID: cid, UserID: target}); err != nil {
		serverError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
