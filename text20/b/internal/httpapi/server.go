package httpapi

import (
	"net/http"
	"strings"

	"github.com/go-chi/chi"

	"signalboard/internal/service"
)

type Server struct {
	svc    *service.Service
	apiKey string // management-side bearer token
	router http.Handler
}

func NewServer(svc *service.Service, managementAPIKey string) *Server {
	s := &Server{svc: svc, apiKey: managementAPIKey}
	s.router = s.routes()
	return s
}

func (s *Server) Handler() http.Handler { return s.router }

// ServeHTTP lets Server itself satisfy http.Handler (delegates to the router).
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.router.ServeHTTP(w, r)
}

func (s *Server) routes() http.Handler {
	r := chi.NewRouter()

	r.Get("/healthz", s.health)

	// Management API (API key).
	r.Group(func(r chi.Router) {
		r.Use(s.requireManagement)
		r.Route("/v1/stores", func(r chi.Router) {
			r.Post("/", s.createStore)
			r.Get("/", s.listStores)
			r.Get("/{storeID}", s.getStore)

			r.Post("/{storeID}/dishes", s.createDish)
			r.Get("/{storeID}/dishes", s.listDishes)
			r.Put("/{storeID}/dishes/{dishID}/price", s.setDishPrice)
			r.Put("/{storeID}/dishes/{dishID}/active", s.setDishActive)
			r.Put("/{storeID}/dishes/{dishID}/threshold", s.setThreshold)

			r.Get("/{storeID}/draft", s.getDraft)
			r.Put("/{storeID}/draft", s.replaceDraft)
			r.Post("/{storeID}/import", s.batchImport)

			r.Post("/{storeID}/publish", s.publish)
			r.Get("/{storeID}/menu", s.getPublishedMenu)
			r.Put("/{storeID}/temp-prices", s.scheduleTempPrices)

			r.Post("/{storeID}/sales", s.ingestSales)
			r.Get("/{storeID}/sales/today", s.todaySales)

			r.Post("/{storeID}/screens", s.registerScreen)
			r.Get("/{storeID}/screens", s.listScreens)
		})
	})

	// Screen API (per-screen token, scoped to its own store).
	r.Group(func(r chi.Router) {
		r.Use(s.requireScreen)
		r.Get("/screen/v1/menu", s.screenMenu)
		r.Post("/screen/v1/heartbeat", s.screenHeartbeat)
	})

	return r
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// ---- auth ----------------------------------------------------------------

func bearer(r *http.Request) string {
	h := r.Header.Get("Authorization")
	if h == "" {
		return ""
	}
	parts := strings.SplitN(h, " ", 2)
	if len(parts) == 2 && strings.EqualFold(parts[0], "Bearer") {
		return strings.TrimSpace(parts[1])
	}
	return ""
}

type ctxKey int

const (
	ctxScreen ctxKey = iota + 1
)

func (s *Server) requireManagement(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if s.apiKey == "" || bearer(r) != s.apiKey {
			writeError(w, http.StatusUnauthorized, "invalid or missing management API key")
			return
		}
		next.ServeHTTP(w, r)
	})
}
