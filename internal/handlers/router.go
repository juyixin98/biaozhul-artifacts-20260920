package handlers

import (
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	chimw "github.com/go-chi/chi/v5/middleware"

	"communitygov/internal/database"
	"communitygov/internal/middleware"
)

type RouterDeps struct {
	Store     *database.Store
	Handlers  *Handlers
	JWTSecret string
	JWTTTL    time.Duration
}

// NewRouter wires all routes. Community resources live under
// /api/communities/{communityID}/... so every authorization decision is scoped.
func NewRouter(d RouterDeps) http.Handler {
	r := chi.NewRouter()
	r.Use(chimw.Recoverer)
	r.Use(middleware.RequestID)
	r.Use(chimw.Logger)

	r.Get("/healthz", func(w http.ResponseWriter, req *http.Request) {
		if err := d.Store.Pool().Ping(req.Context()); err != nil {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	r.Route("/api", func(r chi.Router) {
		r.Post("/register", d.Handlers.Register)
		r.Post("/login", d.Handlers.Login(AuthDeps{Secret: d.JWTSecret, TTL: d.JWTTTL}))

		// Public community listing (still authenticated).
		r.Group(func(r chi.Router) {
			r.Use(auth(d))
			r.Get("/me", d.Handlers.Me)
			r.Get("/communities", d.Handlers.ListCommunities)
		})

		r.Route("/communities/{communityID}", func(r chi.Router) {
			r.Use(auth(d))

			// Admin: community structure and payments.
			r.Group(func(r chi.Router) {
				r.Use(middleware.RequireRole("admin"))
				r.Post("/tiers", d.Handlers.CreateTier)
				r.Post("/payments", d.Handlers.RecordPayment)
				r.Post("/members/{userID}/cancel", d.Handlers.CancelMembership)
				r.Post("/reviewers", d.Handlers.AddReviewer)
			})

			// Any authenticated user.
			r.Get("/tiers", d.Handlers.ListTiers)
			r.Get("/members/{userID}", d.Handlers.GetMembership)

			// Posts.
			r.Post("/posts", d.Handlers.CreatePost)
			r.Get("/posts", d.Handlers.ListPosts)
			r.Get("/posts/{postID}", d.Handlers.GetPost)
			r.Get("/posts/{postID}/versions", d.Handlers.ListVersions)
			r.Post("/posts/{postID}/versions", d.Handlers.EditPost)
			r.Post("/posts/{postID}/submit", d.Handlers.SubmitPost)
			r.Post("/posts/{postID}/withdraw", d.Handlers.WithdrawPost)
			r.Post("/posts/{postID}/review", d.Handlers.ReviewPost)
			r.Post("/posts/{postID}/takedown", d.Handlers.TakedownPost)
			r.Post("/posts/{postID}/restore", d.Handlers.RestorePost)

			// Content reads (unified gate) + export.
			r.Get("/versions/{versionID}", d.Handlers.ReadVersion)
			r.Get("/versions/{versionID}/export", d.Handlers.ExportVersion)
			r.Post("/versions/{versionID}/attachments", d.Handlers.UploadAttachment)
			r.Get("/attachments/{attachmentID}", d.Handlers.DownloadAttachment)

			// Reports.
			r.Post("/reports", d.Handlers.FileReport)
			r.Get("/reports", d.Handlers.ListReports)
			r.Get("/reports/{reportID}", d.Handlers.GetReport)
			r.Get("/reports/{reportID}/decisions", d.Handlers.ReportDecisions)
			r.Post("/reports/{reportID}/accept", d.Handlers.AcceptReport)
			r.Post("/reports/{reportID}/reject", d.Handlers.RejectReport)
			r.Post("/reports/{reportID}/uphold", d.Handlers.UpholdReport)
			r.Post("/reports/{reportID}/overturn", d.Handlers.OverturnReport)
			r.Post("/reports/{reportID}/appeal", d.Handlers.AppealReport)
			r.Post("/reports/{reportID}/restore", d.Handlers.RestoreFromReport)

			// Courses.
			r.Post("/courses", d.Handlers.CreateCourse)
			r.Get("/courses", d.Handlers.ListCourses)
			r.Get("/courses/{courseID}", d.Handlers.GetDraftCourse)
			r.Get("/courses/{courseID}/published", d.Handlers.GetPublishedCourse)
			r.Put("/courses/{courseID}/structure", d.Handlers.SetCourseStructure)
			r.Post("/courses/{courseID}/publish", d.Handlers.PublishCourse)
			r.Post("/courses/{courseID}/unpublish", d.Handlers.UnpublishCourse)
		})
	})

	// Admin-only: create a community (global resource).
	r.With(auth(d), middleware.RequireRole("admin")).
		Post("/api/communities", d.Handlers.CreateCommunity)

	return r
}

func auth(d RouterDeps) func(http.Handler) http.Handler {
	return middleware.Authenticator(d.JWTSecret, d.Handlers.Users.GetByID)
}
