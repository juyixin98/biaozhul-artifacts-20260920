package demo

import (
	"fmt"
	"log"
	"math/rand"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// NewServer builds the local demonstration site. It intentionally exhibits
// measurable behavior: slow resources, layout shifts, long tasks, missing
// subresources, and a whitelisted redirect chain.
func NewServer() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, page("SiteVitals demo — home", `
<h1>SiteVitals local demo</h1>
<ul>
  <li><a href="/normal">/normal</a> — fast baseline page</li>
  <li><a href="/slow">/slow</a> — slow CSS/image + slow response</li>
  <li><a href="/cls">/cls</a> — layout shift after content injection</li>
  <li><a href="/longtask">/longtask</a> — main-thread long tasks</li>
  <li><a href="/partial">/partial</a> — a missing image and 500 subresource</li>
  <li><a href="/redirect/1">/redirect/1</a> — two in-whitelist redirect hops</li>
</ul>`))
	})

	mux.HandleFunc("/normal", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, page("Normal", `
<h1>Normal page</h1>
<p>Above-the-fold content renders immediately. No layout shifts.</p>
<img src="/static/img?w=600&h=200" width="600" height="200" alt="hero">
`))
	})

	mux.HandleFunc("/slow", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, page("Slow", `
<link rel="stylesheet" href="/static/slow.css?ms=1200">
<h1>Slow page</h1>
<p>Stylesheet blocks rendering for ~1.2s; hero image is ~800ms late.</p>
<img src="/static/img?w=600&h=300&ms=800" width="600" height="300" alt="slow hero">
`))
	})

	mux.HandleFunc("/cls", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, page("CLS", `
<h1>Layout shift page</h1>
<div id="banner-slot" style="min-height:0"></div>
<p>This text starts near the top…</p>
<p>…and gets pushed down when the banner is injected after 600ms.</p>
<script>
setTimeout(function () {
  var el = document.getElementById('banner-slot');
  el.innerHTML = '<div style="height:160px;background:#4a90d9;color:#fff;display:flex;align-items:center;justify-content:center">Late banner causing CLS</div>';
}, 600);
</script>
`))
	})

	mux.HandleFunc("/longtask", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, page("Long tasks", `
<h1>Long task page</h1>
<p>The page blocks the main thread in 120ms chunks after load.</p>
<script>
setTimeout(function () {
  for (var round = 0; round < 4; round++) {
    var start = performance.now();
    while (performance.now() - start < 120) {}
  }
}, 400);
</script>
`))
	})

	mux.HandleFunc("/partial", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, page("Partial failures", `
<h1>Partial failures</h1>
<p>This image 404s (subresource failure does not fail the task):</p>
<img src="/static/missing.png" alt="missing">
<p>And this endpoint returns 500:</p>
<img src="/static/broken" alt="broken">
<p>…but the page itself loads and metrics are still collected.</p>
`))
	})

	// Two-hop redirect chain, all hops served by the demo (whitelisted).
	mux.HandleFunc("/redirect/1", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/redirect/2", http.StatusFound)
	})
	mux.HandleFunc("/redirect/2", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/redirect/final", http.StatusMovedPermanently)
	})
	mux.HandleFunc("/redirect/final", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, page("Redirect target", `
<h1>Landed after two redirects</h1>
<p>Every hop was served by this whitelisted origin.</p>`))
	})

	// Bad redirects, for negative tests: hop to a non-HTTP scheme / off-list host.
	mux.HandleFunc("/badredirect-file", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "file:///etc/passwd", http.StatusFound)
	})
	mux.HandleFunc("/badredirect-offsite", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://evil.invalid/x", http.StatusFound)
	})
	// Endless in-whitelist redirect loop for the redirect-limit test.
	mux.HandleFunc("/redirect/loop/", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/redirect/loop/next", http.StatusFound)
	})

	// /external?img=<absolute-url> embeds an arbitrary subresource. Browser
	// tests use it with a second server to exercise subresource whitelist
	// policy (record-only vs STRICT_SUBRESOURCES block).
	mux.HandleFunc("/external", func(w http.ResponseWriter, r *http.Request) {
		img := r.URL.Query().Get("img")
		if img == "" {
			img = "/static/img?w=100&h=100"
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, page("External subresource", fmt.Sprintf(`
<h1>External subresource page</h1>
<img src="%s" width="100" height="100" alt="external">
<p>End.</p>`, img)))
	})

	// /hang never completes a response: used by the navigation-timeout test.
	mux.HandleFunc("/hang", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		fl, _ := w.(http.Flusher)
		if fl != nil {
			w.WriteHeader(200)
			// A partial body starts the response but the document load never
			// completes, so the browser keeps waiting for the load event.
			_, _ = w.Write([]byte("<html><body>hanging"))
			fl.Flush()
		}
		select {
		case <-r.Context().Done():
		case <-time.After(60 * time.Second):
		}
	})

	// Static-ish asset server with artificial latency and SVG images.
	mux.HandleFunc("/static/", func(w http.ResponseWriter, r *http.Request) {
		ms := delay(r.URL.Query().Get("ms"))
		if ms > 0 {
			time.Sleep(ms)
		}
		switch {
		case strings.HasPrefix(r.URL.Path, "/static/slow.css"):
			w.Header().Set("Content-Type", "text/css")
			fmt.Fprint(w, "body{font-family:sans-serif;margin:24px;color:#222}h1{color:#0b5}")
		case strings.HasPrefix(r.URL.Path, "/static/img"):
			width, _ := strconv.Atoi(r.URL.Query().Get("w"))
			height, _ := strconv.Atoi(r.URL.Query().Get("h"))
			if width == 0 {
				width = 400
			}
			if height == 0 {
				height = 200
			}
			w.Header().Set("Content-Type", "image/svg+xml")
			fmt.Fprintf(w, svg(width, height))
		case r.URL.Path == "/static/broken":
			http.Error(w, "simulated 500", http.StatusInternalServerError)
		default:
			http.NotFound(w, r)
		}
	})

	return logRequests(mux)
}

func delay(v string) time.Duration {
	n, err := strconv.Atoi(v)
	if err != nil || n < 0 || n > 10000 {
		return 0
	}
	return time.Duration(n) * time.Millisecond
}

func page(title, body string) string {
	return fmt.Sprintf(`<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title>
<style>body{font-family:sans-serif;margin:24px;color:#222;background:#fafafa}h1{color:#234}a{color:#06c}</style>
</head>
<body>%s</body></html>`, title, body)
}

func svg(w, h int) string {
	c1 := 40 + rand.Intn(120)
	c2 := 100 + rand.Intn(120)
	c3 := 150 + rand.Intn(100)
	return fmt.Sprintf(`<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">
<rect width="100%%" height="100%%" fill="rgb(%d,%d,%d)"/>
<text x="50%%" y="50%%" dominant-baseline="middle" text-anchor="middle" fill="white" font-size="28" font-family="sans-serif">demo %d×%d</text>
</svg>`, w, h, w, h, c1, c2, c3, w, h)
}

func logRequests(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		log.Printf("demo: %s %s", r.Method, r.URL)
		h.ServeHTTP(w, r)
	})
}
