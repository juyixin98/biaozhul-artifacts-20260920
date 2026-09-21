// Package testsite is the built-in demo web server used to exercise
// navigation, resource waterfall, long tasks, layout shift, redirects,
// cross-origin (blocked) subresources and partial resource failures.
package testsite

import (
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Server wraps an http.Server for the demo site.
type Server struct {
	Addr    string
	handler http.Handler
	httpd   *http.Server
}

// New constructs the demo site bound to addr (e.g. ":8093").
func New(addr string) *Server {
	mux := http.NewServeMux()
	s := &Server{Addr: addr, handler: mux}

	mux.HandleFunc("/", s.index)
	mux.HandleFunc("/slow", s.slow)
	mux.HandleFunc("/longtask", s.longTaskPage)
	mux.HandleFunc("/cls", s.clsPage)
	mux.HandleFunc("/missing", s.missingPage)
	mux.HandleFunc("/redirect", s.redirect)
	mux.HandleFunc("/external", s.externalPage)
	mux.HandleFunc("/asset/", s.asset)
	mux.HandleFunc("/hang", s.hang)
	return s
}

// Start listens in the background.
func (s *Server) Start() error {
	s.httpd = &http.Server{Addr: s.Addr, Handler: s.handler, ReadHeaderTimeout: 5 * time.Second}
	ln, err := newListener(s.Addr)
	if err != nil {
		return err
	}
	go func() {
		if err := s.httpd.Serve(ln); err != nil && err != http.ErrServerClosed {
			log.Printf("testsite: %v", err)
		}
	}()
	return nil
}

// Shutdown gracefully stops the server.
func (s *Server) Shutdown(ctx context.Context) error {
	if s.httpd == nil {
		return nil
	}
	return s.httpd.Shutdown(ctx)
}

const basePage = `<!doctype html><html><head><meta charset="utf-8"><title>%s</title>
<link rel="stylesheet" href="/asset/style.css">
<script src="/asset/app.js" defer></script>
</head><body>
<h1>%s</h1>
<p id="intro">%s</p>
<img src="/asset/logo.png" alt="logo">
%s
</body></html>`

func (s *Server) index(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprintf(w, basePage, "SiteVitals Test Site", "SiteVitals Test Site",
		"Static page with CSS/JS/image subresources.",
		`<p><a href="/slow">slow page</a> | <a href="/longtask">long tasks</a> |
<a href="/cls">layout shift</a> | <a href="/redirect">redirect chain</a> |
<a href="/external">external (blocked)</a> | <a href="/missing">partial failure</a> |
<a href="/hang">hang (timeout)</a></p>`)
}

func (s *Server) slow(w http.ResponseWriter, r *http.Request) {
	ms := 1500
	if v := r.URL.Query().Get("ms"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 && n < 60000 {
			ms = n
		}
	}
	time.Sleep(time.Duration(ms) * time.Millisecond)
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprintf(w, basePage, "Slow Page", "Slow Page",
		"Server delayed the response by "+strconv.Itoa(ms)+" ms.", "")
}

func (s *Server) longTaskPage(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprintf(w, basePage, "Long Tasks", "Long Task Page",
		"Two blocking tasks (>50 ms) run on load and after paint.",
		`<div id="box" style="height:80px;background:#eee">working…</div>
<script>
function block(ms){ var t=performance.now(); while(performance.now()-t<ms){} }
block(220);
setTimeout(function(){ block(180); document.getElementById('box').textContent='done'; }, 600);
</script>`)
}

func (s *Server) clsPage(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprintf(w, basePage, "CLS Demo", "Layout Shift Page",
		"An element inserted late pushes content down (expected CLS > 0).",
		`<div id="push" style="min-height:0"></div>
<div style="font-size:20px">Content that will move</div>
<script>
setTimeout(function(){
  var d=document.createElement('div');
  d.style.cssText='height:120px;background:#fdd;padding:8px';
  d.textContent='Inserted late — causes a layout shift.';
  document.getElementById('push').appendChild(d);
}, 700);
</script>`)
}

func (s *Server) missingPage(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprintf(w, basePage, "Partial Failure", "Partial Resource Failure",
		"This page references an image that 404s; the run succeeds and records the failure.",
		`<img src="/asset/does-not-exist.png" alt="broken">
<script src="/asset/gone.js" defer></script>`)
}

func (s *Server) redirect(w http.ResponseWriter, r *http.Request) {
	n := 0
	if v := r.URL.Query().Get("n"); v != "" {
		n, _ = strconv.Atoi(v)
	}
	// Default: hop -> /redirect?n=1 ... up to 6 then / => exercises the 5-hop limit.
	if n >= 6 {
		http.Redirect(w, r, "/", http.StatusFound)
		return
	}
	http.Redirect(w, r, "/redirect?n="+strconv.Itoa(n+1), http.StatusFound)
}

func (s *Server) externalPage(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprintf(w, basePage, "External", "External Subresource",
		"This page loads subresources from origins NOT on the allow-list; they must be blocked.",
		`<img src="https://example.com/not-allowed.png" alt="x">
<link rel="stylesheet" href="https://cdn.example.com/x.css">
<script src="http://127.0.0.1:9/also-blocked.js" defer></script>`)
}

func (s *Server) hang(w http.ResponseWriter, r *http.Request) {
	// Never respond; the collector's navigation timeout must fire.
	fl, _ := w.(http.Flusher)
	if fl != nil {
		w.WriteHeader(http.StatusOK)
		fl.Flush()
	}
	<-r.Context().Done()
}

func (s *Server) asset(w http.ResponseWriter, r *http.Request) {
	name := strings.TrimPrefix(r.URL.Path, "/asset/")
	switch {
	case name == "style.css":
		w.Header().Set("Content-Type", "text/css")
		io.WriteString(w, "body{font-family:sans-serif;margin:24px}h1{color:#2a4}\n")
	case name == "app.js":
		w.Header().Set("Content-Type", "application/javascript")
		io.WriteString(w, "document.addEventListener('DOMContentLoaded',function(){});\n")
	case name == "logo.png":
		w.Header().Set("Content-Type", "image/png")
		w.Write(pixelPNG())
	default:
		http.NotFound(w, r)
	}
}

// pixelPNG returns a tiny valid 1x1 PNG.
func pixelPNG() []byte {
	return []byte{
		0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D,
		0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
		0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4, 0x89, 0x00, 0x00, 0x00,
		0x0A, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9C, 0x62, 0x00, 0x01, 0x00, 0x00,
		0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00, 0x00, 0x00, 0x00, 0x49,
		0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
	}
}
