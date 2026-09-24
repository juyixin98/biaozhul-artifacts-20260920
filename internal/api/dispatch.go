package api

import (
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"
)

// repoHandler is a repository-scoped handler. The repository and any path
// parameters are provided through the chi routing context as usual.
type repoHandler func(http.ResponseWriter, *http.Request)

type repoRoute struct {
	method    string
	suffix    string // trailing action relative to the repository, e.g. "tags/{tag}"
	paramName string // name of the "{...}" single segment, "" when static
	static    string // literal prefix of the suffix, e.g. "tags/"
	handler   repoHandler
}

// routes is ordered most-specific (longest static prefix) first so a request
// for "/tags/foo" cannot be mistaken for a repository literally named "tags".
func (s *Server) routes() []repoRoute {
	return []repoRoute{
		{http.MethodPut, "tags/{tag}", "tag", "tags/", s.putTag},
		{http.MethodGet, "blobs/{digest}", "digest", "blobs/", s.getBlob},
		{http.MethodPut, "blobs/{digest}", "digest", "blobs/", s.putBlob},
		{http.MethodGet, "tasks/{id}", "id", "tasks/", s.getTask},
		{http.MethodGet, "tags", "", "tags", s.listTags},
		{http.MethodPost, "resolve", "", "resolve", s.resolve},
		{http.MethodGet, "tasks", "", "tasks", s.listTasks},
	}
}

// repoDispatch splits "/v1/repos/<repository>/<action>" and invokes the
// matching handler. The repository is the longest prefix that leaves a known
// action suffix; matching is method-aware.
func (s *Server) repoDispatch(w http.ResponseWriter, r *http.Request) {
	rest := strings.TrimPrefix(r.URL.Path, "/v1/repos/")
	if rest == "" || strings.HasSuffix(rest, "/") {
		writeError(w, http.StatusNotFound, "not_found", "repository-scoped path required")
		return
	}

	for _, rt := range s.routes() {
		if rt.method != r.Method {
			continue
		}
		if rt.paramName != "" {
			// Parameterized: rest = <repo>/<static><value>, value one segment.
			tail := "/" + rt.static
			at := strings.LastIndex(rest, tail)
			if at <= 0 {
				continue
			}
			repo := rest[:at]
			value := rest[at+len(tail):]
			if repo == "" || value == "" || strings.Contains(value, "/") {
				continue
			}
			s.serveWithParams(w, r, rt, repo, value)
			return
		}

		// Static: rest = <repo>/<static>, repository at least one segment.
		tail := "/" + rt.static
		if strings.HasSuffix(rest, tail) {
			repo := strings.TrimSuffix(rest, tail)
			if repo != "" {
				s.serveWithParams(w, r, rt, repo, "")
				return
			}
		}
	}

	writeError(w, http.StatusNotFound, "not_found",
		"no repository-scoped route for "+r.Method+" "+r.URL.Path)
}

// serveWithParams injects repo and the optional path parameter into a fresh chi
// routing context, so handlers keep using chi.URLParam unchanged.
func (s *Server) serveWithParams(w http.ResponseWriter, r *http.Request, rt repoRoute, repo, paramValue string) {
	rctx := chi.NewRouteContext()
	rctx.URLParams.Add("repo", repo)
	if rt.paramName != "" {
		rctx.URLParams.Add(rt.paramName, paramValue)
	}
	rt.handler(w, r.WithContext(chiCtx(r.Context(), rctx)))
}
