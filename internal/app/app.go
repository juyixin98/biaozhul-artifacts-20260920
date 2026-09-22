package app

import (
	"context"
	"errors"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"community-governance/internal/database"
)

// App holds shared dependencies for every HTTP handler.
type App struct {
	Pool           *pgxpool.Pool
	Q              *database.Queries
	BootstrapToken string
}

func New(pool *pgxpool.Pool) *App {
	return &App{Pool: pool, Q: database.New(pool)}
}

// NewWithBootstrapToken enables a synthetic platform-admin token used to
// create communities on an empty system.
func NewWithBootstrapToken(pool *pgxpool.Pool, token string) *App {
	return &App{Pool: pool, Q: database.New(pool), BootstrapToken: token}
}

// ctxKey is unexported so only this package can touch context values.
type ctxKey int

const (
	ctxUser ctxKey = iota
)

// CurrentUser is the authenticated user placed in request context.
type CurrentUser struct {
	ID          int64
	CommunityID int64
	Username    string
	Role        string
}

func (a *App) authMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := r.Header.Get("Authorization")
		if len(token) > 7 && token[:7] == "Bearer " {
			token = token[7:]
		}
		if token == "" {
			token = r.URL.Query().Get("token")
		}
		if token == "" {
			unauthorized(w, "missing bearer token")
			return
		}
		// Synthetic platform-admin token: can create communities, nothing else
		// (every community-scoped route rejects community_id 0).
		if a.BootstrapToken != "" && token == a.BootstrapToken {
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), ctxUser, CurrentUser{
				ID: 0, CommunityID: 0, Username: "platform", Role: "admin",
			})))
			return
		}
		u, err := a.Q.GetUserByToken(r.Context(), token)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				unauthorized(w, "invalid token")
				return
			}
			serverError(w, err)
			return
		}
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), ctxUser, CurrentUser{
			ID:          u.ID,
			CommunityID: u.CommunityID,
			Username:    u.Username,
			Role:        u.Role,
		})))
	})
}

func currentUser(r *http.Request) CurrentUser {
	return r.Context().Value(ctxUser).(CurrentUser)
}

func (u CurrentUser) isAdmin() bool     { return u.Role == "admin" }
func (u CurrentUser) isModerator() bool { return u.Role == "moderator" || u.Role == "admin" }

// withTx runs fn inside a transaction, committing on nil error.
func (a *App) withTx(ctx context.Context, fn func(q *database.Queries) error) error {
	tx, err := a.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	if err := fn(database.New(tx)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// Router wires all routes.
func (a *App) Router() http.Handler {
	r := chi.NewRouter()
	r.Get("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})

	r.Group(func(r chi.Router) {
		r.Use(a.authMiddleware)

		// Communities & bootstrap (admin only inside the target community)
		r.Post("/admin/communities", a.createCommunity)
		r.Post("/communities/{cid}/users", a.createUser)
		r.Get("/communities/{cid}/users", a.listUsers)

		// Tiers
		r.Post("/communities/{cid}/tiers", a.createTier)
		r.Get("/communities/{cid}/tiers", a.listTiers)
		r.Post("/communities/{cid}/tiers/{id}/active", a.setTierActive)

		// Membership & payments
		r.Post("/communities/{cid}/payments", a.registerPayment)
		r.Get("/communities/{cid}/me/subscription", a.mySubscription)
		r.Post("/communities/{cid}/me/cancel", a.cancelMySubscription)

		// Contents
		r.Post("/communities/{cid}/contents", a.createContent)
		r.Get("/communities/{cid}/contents", a.listContents)
		r.Get("/contents/{id}", a.getContent)
		r.Post("/contents/{id}/versions", a.createVersion)
		r.Get("/contents/{id}/versions", a.listVersions)
		r.Get("/versions/{id}", a.getVersion)
		r.Post("/contents/{id}/submit", a.submitContent)
		r.Post("/contents/{id}/approve", a.approveContent)
		r.Post("/contents/{id}/reject", a.rejectContent)
		r.Post("/contents/{id}/delist", a.delistContent)
		r.Post("/contents/{id}/restore", a.restoreContent)
		r.Get("/contents/{id}/events", a.listContentEvents)

		// Attachments (bound to a frozen version)
		r.Post("/versions/{id}/attachments", a.addAttachment)
		r.Get("/versions/{id}/attachments", a.listAttachments)
		r.Get("/attachments/{id}", a.getAttachment)
		r.Get("/contents/{id}/export", a.exportContent)

		// Reports
		r.Post("/contents/{id}/reports", a.createReport)
		r.Get("/communities/{cid}/reports", a.listReports)
		r.Get("/reports/{id}", a.getReport)
		r.Post("/reports/{id}/accept", a.acceptReport)
		r.Post("/reports/{id}/uphold", a.upholdReport)
		r.Post("/reports/{id}/dismiss", a.dismissReport)
		r.Post("/reports/{id}/appeal", a.appealReport)
		r.Post("/reports/{id}/appeal/uphold", a.appealUphold)
		r.Post("/reports/{id}/appeal/dismiss", a.appealDismiss)
		r.Get("/reports/{id}/events", a.listReportEvents)

		// Courses
		r.Post("/communities/{cid}/courses", a.createCourse)
		r.Get("/communities/{cid}/courses", a.listCourses)
		r.Get("/courses/{id}", a.getCourse)
		r.Put("/courses/{id}/structure", a.replaceCourseStructure)
		r.Post("/courses/{id}/publish", a.publishCourse)
		r.Get("/courses/{id}/lessons/{pos}", a.getLesson)
	})

	return r
}
