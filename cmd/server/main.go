// Command server runs the conditional-update HTTP service locally.
// All external dependencies are in-process fakes; nothing talks to any
// production system.
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"conditionupdate/internal/clock"
	"conditionupdate/internal/fakesvc"
	"conditionupdate/internal/httpserver"
	"conditionupdate/internal/store"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	useFakeClock := flag.Bool("fake-clock", true, "use the controllable fake clock (enables /admin/clock)")
	flag.Parse()

	var c clock.Clock = clock.Real{}
	if *useFakeClock {
		c = clock.NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	}
	audit := fakesvc.NewAudit(c)
	srv := httpserver.New(store.New(c), audit, c)

	log.Printf("listening on http://%s (fake-clock=%v)", *addr, *useFakeClock)
	log.Fatal(http.ListenAndServe(*addr, srv))
}
