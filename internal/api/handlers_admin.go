package api

import (
	"encoding/json"
	"errors"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"

	"github.com/clearsettle/clearsettle/internal/admin"
	"github.com/clearsettle/clearsettle/internal/auth"
	"github.com/clearsettle/clearsettle/internal/store"
)

func adminActor(r *http.Request) admin.Actor {
	p := principalFrom(r.Context())
	return admin.Actor{UserID: p.UserID, Role: p.Role, IP: clientIP(r)}
}

func writeAdminError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, admin.ErrNotFound):
		writeError(w, http.StatusNotFound, "not_found", err.Error())
	case errors.Is(err, admin.ErrInvalidInput):
		writeError(w, http.StatusBadRequest, "invalid_input", "check name, fee schedule, and password (min 10 chars)")
	case errors.Is(err, admin.ErrEmailTaken):
		writeError(w, http.StatusConflict, "email_taken", err.Error())
	default:
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
	}
}

type createMerchantReq struct {
	Name     string `json:"name"`
	FeeBps   int32  `json:"fee_bps"`
	FeeFixed int64  `json:"fee_fixed"`
}

func (d Deps) handleCreateMerchant(w http.ResponseWriter, r *http.Request) {
	var req createMerchantReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", err.Error())
		return
	}
	res, err := d.Admin.CreateMerchant(r.Context(), admin.CreateMerchantInput{
		Name: req.Name, FeeBps: req.FeeBps, FeeFixed: req.FeeFixed,
	}, adminActor(r))
	if err != nil {
		writeAdminError(w, err)
		return
	}
	// The plaintext key is returned exactly once, in this response.
	writeJSON(w, http.StatusCreated, map[string]any{
		"merchant": map[string]any{
			"id":           res.Merchant.ID.String(),
			"name":         res.Merchant.Name,
			"fee_bps":      res.Merchant.FeeBps,
			"fee_fixed":    res.Merchant.FeeFixed,
			"status":       res.Merchant.Status,
			"api_key_mask": auth.MaskAPIKey(res.Merchant.ApiKeyPrefix.String),
		},
		"api_key": res.APIKey,
		"warning": "Store this API key now. It is shown only once and cannot be recovered.",
	})
}

func (d Deps) handleListMerchants(w http.ResponseWriter, r *http.Request) {
	rows, err := d.Admin.ListMerchants(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, m := range rows {
		out = append(out, map[string]any{
			"id":           m.ID.String(),
			"name":         m.Name,
			"fee_bps":      m.FeeBps,
			"fee_fixed":    m.FeeFixed,
			"status":       m.Status,
			"api_key_mask": auth.MaskAPIKey(m.ApiKeyPrefix.String),
			"created_at":   m.CreatedAt.Time.Format("2006-01-02T15:04:05Z07:00"),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"merchants": out})
}

func (d Deps) handleRotateKey(w http.ResponseWriter, r *http.Request) {
	id, err := uuid.Parse(chi.URLParam(r, "id"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_id", "merchant id must be a UUID")
		return
	}
	key, err := d.Admin.RotateAPIKey(r.Context(), id, adminActor(r))
	if err != nil {
		writeAdminError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"api_key": key,
		"warning": "Previous key is now invalid. Store the new key now; it is shown only once.",
	})
}

type statusReq struct {
	Status string `json:"status"`
}

func (d Deps) handleMerchantStatus(w http.ResponseWriter, r *http.Request) {
	id, err := uuid.Parse(chi.URLParam(r, "id"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_id", "merchant id must be a UUID")
		return
	}
	var req statusReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", err.Error())
		return
	}
	if err := d.Admin.SetMerchantStatus(r.Context(), id, req.Status, adminActor(r)); err != nil {
		writeAdminError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"id": id.String(), "status": req.Status})
}

type createUserReq struct {
	Email      string `json:"email"`
	Password   string `json:"password"`
	Role       string `json:"role"`
	MerchantID string `json:"merchant_id"`
}

func (d Deps) handleCreateUser(w http.ResponseWriter, r *http.Request) {
	var req createUserReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", err.Error())
		return
	}
	in := admin.CreateUserInput{Email: req.Email, Password: req.Password, Role: req.Role}
	if req.MerchantID != "" {
		mid, err := uuid.Parse(req.MerchantID)
		if err != nil {
			writeError(w, http.StatusBadRequest, "bad_merchant_id", "merchant_id must be a UUID")
			return
		}
		in.MerchantID = &mid
	}
	u, err := d.Admin.CreateUser(r.Context(), in, adminActor(r))
	if err != nil {
		writeAdminError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"id": u.ID.String(), "email": auth.MaskEmail(u.Email), "role": u.Role,
		"merchant_id": u.MerchantID,
	})
}

func (d Deps) handleListUsers(w http.ResponseWriter, r *http.Request) {
	p := principalFrom(r.Context())
	var rows []store.User
	var err error
	if mid := r.URL.Query().Get("merchant_id"); mid != "" {
		id, perr := uuid.Parse(mid)
		if perr != nil {
			writeError(w, http.StatusBadRequest, "bad_merchant_id", "merchant_id must be a UUID")
			return
		}
		rows, err = d.Q.ListUsersByMerchant(r.Context(), &id)
	} else {
		rows, err = d.Q.ListUsers(r.Context())
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, u := range rows {
		out = append(out, map[string]any{
			"id": u.ID.String(), "email": auth.MaskEmail(u.Email), "role": u.Role,
			"merchant_id": u.MerchantID, "status": u.Status,
		})
	}
	_ = p
	writeJSON(w, http.StatusOK, map[string]any{"users": out})
}
