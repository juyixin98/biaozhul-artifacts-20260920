package httpapi

import (
	"context"
	"net/http"

	"signalboard/internal/db"
	"signalboard/internal/service"
)

type screenCtx struct {
	Screen db.Screen
}

func (s *Server) requireScreen(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := bearer(r)
		if token == "" {
			writeError(w, http.StatusUnauthorized, "missing screen token")
			return
		}
		screen, err := s.svc.AuthenticateScreen(r.Context(), token)
		if err != nil {
			code := http.StatusInternalServerError
			if err == service.ErrUnauthorized {
				code = http.StatusUnauthorized
			}
			writeError(w, code, err.Error())
			return
		}
		ctx := context.WithValue(r.Context(), ctxScreen, screen)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func screenFromCtx(r *http.Request) db.Screen {
	return r.Context().Value(ctxScreen).(db.Screen)
}
