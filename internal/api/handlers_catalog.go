package api

import (
	"encoding/json"
	"errors"
	"net/http"

	"github.com/go-chi/chi/v5"

	"costlens/internal/auth"
	"costlens/internal/csvparse"
	"costlens/internal/httpxx"
	"costlens/internal/service"
)

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		httpxx.Error(w, http.StatusBadRequest, "invalid JSON body: "+err.Error(), nil)
		return false
	}
	return true
}

// ---------- Catalog ----------

func (h *Handler) listOrgs(w http.ResponseWriter, r *http.Request) {
	p := auth.FromContext(r.Context())
	if p.IsAdmin() {
		os, err := h.svc.ListOrgs(r.Context())
		if err != nil {
			httpxx.Error(w, 500, err.Error(), nil)
			return
		}
		httpxx.JSON(w, 200, os)
		return
	}
	// Non-admins only see their granted organizations.
	all, err := h.svc.ListOrgs(r.Context())
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	out := make([]*service.OrgDTO, 0)
	for _, o := range all {
		if p.CanAccessOrg(o.ID) {
			out = append(out, o)
		}
	}
	httpxx.JSON(w, 200, out)
}

func (h *Handler) listCostCenters(w http.ResponseWriter, r *http.Request) {
	p := auth.FromContext(r.Context())
	orgIDs := p.OrgIDs()
	// empty slice vs nil matters: sqlc treats nil slice as no filter.
	if !p.IsAdmin() && len(orgIDs) == 0 {
		httpxx.JSON(w, 200, []any{})
		return
	}
	ccs, err := h.svc.ListCostCenters(r.Context(), orgIDs)
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 200, ccs)
}

func (h *Handler) listAccounts(w http.ResponseWriter, r *http.Request) {
	p := auth.FromContext(r.Context())
	as, err := h.svc.ListAccounts(r.Context(), p.OrgIDs())
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 200, as)
}

func (h *Handler) createOrg(w http.ResponseWriter, r *http.Request) {
	var in service.CreateOrgInput
	if !decodeJSON(w, r, &in) {
		return
	}
	o, err := h.svc.CreateOrg(r.Context(), in)
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 201, o)
}

func (h *Handler) createCostCenter(w http.ResponseWriter, r *http.Request) {
	var in service.CreateCostCenterInput
	if !decodeJSON(w, r, &in) {
		return
	}
	cc, err := h.svc.CreateCostCenter(r.Context(), in)
	if errors.Is(err, service.ErrNotFound) {
		httpxx.Error(w, 404, "organization not found", nil)
		return
	}
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 201, cc)
}

func (h *Handler) createAccount(w http.ResponseWriter, r *http.Request) {
	var in service.CreateAccountInput
	if !decodeJSON(w, r, &in) {
		return
	}
	a, err := h.svc.CreateAccount(r.Context(), in)
	if errors.Is(err, service.ErrNotFound) {
		httpxx.Error(w, 404, "organization or cost center not found", nil)
		return
	}
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 201, a)
}

func (h *Handler) createUser(w http.ResponseWriter, r *http.Request) {
	var in service.CreateUserInput
	if !decodeJSON(w, r, &in) {
		return
	}
	if in.Role != "admin" && in.Role != "analyst" && in.Role != "viewer" {
		httpxx.Error(w, 400, "role must be admin, analyst or viewer", nil)
		return
	}
	username, token, err := h.svc.CreateUser(r.Context(), in)
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	// Token is shown exactly once.
	httpxx.JSON(w, 201, map[string]string{"username": username, "api_token": token})
}

func (h *Handler) grant(w http.ResponseWriter, r *http.Request) {
	username := chi.URLParam(r, "username")
	var body struct {
		OrgExternalID string `json:"org_external_id"`
	}
	if !decodeJSON(w, r, &body) {
		return
	}
	if err := h.svc.Grant(r.Context(), username, body.OrgExternalID); err != nil {
		if errors.Is(err, service.ErrNotFound) {
			httpxx.Error(w, 404, "user or organization not found", nil)
			return
		}
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 200, map[string]string{"status": "granted"})
}

// ---------- Import ----------

func (h *Handler) importCSV(w http.ResponseWriter, r *http.Request) {
	p := auth.FromContext(r.Context())
	accountExt := chi.URLParam(r, "externalID")
	account, err := h.svc.AccountForImport(r.Context(), accountExt)
	if errors.Is(err, service.ErrNotFound) {
		httpxx.Error(w, 404, "account not found", nil)
		return
	}
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	if !p.CanImportOrg(account.OrgID) {
		httpxx.Error(w, 403, "not authorized to import into this organization", nil)
		return
	}

	filename, body, err := readUpload(w, r)
	if err != nil {
		httpxx.Error(w, 400, err.Error(), nil)
		return
	}
	defer body.Close()

	rows, perr := csvparse.Parse(body)
	if perr != nil {
		var pe *csvparse.ParseError
		if errors.As(perr, &pe) {
			httpxx.Error(w, 422, "CSV validation failed; batch not imported", pe.Errors)
			return
		}
		httpxx.Error(w, 400, perr.Error(), nil)
		return
	}

	res, err := h.svc.Import(r.Context(), accountExt, filename, p.ID, rows)
	if err != nil {
		var ce *service.ConflictError
		if errors.As(err, &ce) {
			httpxx.Error(w, 409, "content conflict for existing key(s); entire batch rolled back",
				map[string]any{"conflicts": ce.Conflicts})
			return
		}
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 200, res)
}

func (h *Handler) listBatches(w http.ResponseWriter, r *http.Request) {
	p := auth.FromContext(r.Context())
	limit, offset := parseLimitOffset(r)
	bs, err := h.svc.ListBatches(r.Context(), service.Visibility{OrgIDs: p.OrgIDs()}, limit, offset)
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 200, bs)
}

func (h *Handler) rebuild(w http.ResponseWriter, r *http.Request) {
	p := auth.FromContext(r.Context())
	id, err := h.svc.Rebuild(r.Context(), p.ID)
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 200, map[string]any{"rebuild_event_id": id, "status": "completed"})
}
