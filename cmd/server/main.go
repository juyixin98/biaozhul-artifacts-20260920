// Command server runs the local HTTP service demonstrating four-phase graceful
// shutdown. It binds to localhost only; its "external dependency" is a fake
// service living inside the same process. Nothing here talks to production.
package main

import (
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"

	"gracefulshutdown/internal/app"
)

func main() {
	cfg := app.DefaultConfig()
	flag.StringVar(&cfg.PublicAddr, "public", cfg.PublicAddr, "traffic listener address")
	flag.StringVar(&cfg.AdminAddr, "admin", cfg.AdminAddr, "probe/admin listener address")
	flag.DurationVar(&cfg.RejectWindow, "reject-window", cfg.RejectWindow, "phase-1 window: listener stays open but new work gets HTTP 503")
	flag.DurationVar(&cfg.DrainTimeout, "drain", cfg.DrainTimeout, "phase 2 drain budget")
	flag.DurationVar(&cfg.CancelTimeout, "cancel", cfg.CancelTimeout, "phase 3 cancel budget")
	flag.DurationVar(&cfg.CloseTimeout, "close", cfg.CloseTimeout, "per-resource close budget")
	flag.Parse()

	a := app.New(cfg)
	if err := a.Start(); err != nil {
		log.Fatalf("start: %v", err)
	}

	log.Printf("traffic listener : http://%s  (/work /stream /bg)", cfg.PublicAddr)
	log.Printf("admin listener   : http://%s  (/readyz /livez /report /trigger-shutdown)", cfg.AdminAddr)
	log.Printf("budgets: drain=%s cancel=%s close=%s", cfg.DrainTimeout, cfg.CancelTimeout, cfg.CloseTimeout)

	sigs := make(chan os.Signal, 4)
	signal.Notify(sigs, syscall.SIGINT, syscall.SIGTERM)

	// Shutdown can be started either by an OS signal or an HTTP trigger. This
	// loop forwards OS signals (the first one starts, repeats escalate) and
	// exits as soon as the HTTP-triggered (or signal-triggered) run finishes.
	go func() {
		for {
			select {
			case s := <-sigs:
				log.Printf("received %v — forwarding shutdown signal", s)
				go a.Shutdown()
			case <-a.Coordinator().Done():
				return
			}
		}
	}()

	<-a.Coordinator().Done()
	signal.Stop(sigs)
	rep := a.Coordinator().Report()

	out, err := rep.JSON()
	if err != nil {
		log.Printf("encode report: %v", err)
		return
	}
	log.Printf("shutdown report:\n%s", string(out))
}
