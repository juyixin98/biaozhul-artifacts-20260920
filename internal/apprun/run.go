// Package apprun wires a node (coordinator or participant) to an HTTP server
// with graceful shutdown.
package apprun

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"

	"tpc/internal/coordinator"
	"tpc/internal/participant"
)

// RunParticipant starts a participant HTTP server until ctx is cancelled.
func RunParticipant(ctx context.Context, name, listen, datadir string) error {
	st, err := participant.New(name, datadir)
	if err != nil {
		return err
	}
	defer st.Close()

	logger := log.New(log.Writer(), fmt.Sprintf("[%s] ", name), log.LstdFlags)
	if blocked := st.Blocked(); len(blocked) > 0 {
		logger.Printf("recovery: %d prepared txn(s) blocked, waiting for coordinator", len(blocked))
	}
	return serve(ctx, listen, st.Handler(), logger)
}

// RunCoordinator starts a coordinator HTTP server and its background
// recovery loop until ctx is cancelled. participantList is a comma-separated
// list of "name=baseURL" entries, e.g. "p1=http://127.0.0.1:9101,p2=...".
func RunCoordinator(ctx context.Context, name, listen, datadir, participantList string) error {
	parts, err := parseParticipants(participantList)
	if err != nil {
		return err
	}
	logger := log.New(log.Writer(), fmt.Sprintf("[%s] ", name), log.LstdFlags)
	st, err := coordinator.New(name, datadir, parts, logger)
	if err != nil {
		return err
	}
	defer st.Close()

	// Startup sweep is deliberately short: a participant that is still down
	// must not block serving. The 500ms loop below keeps re-driving forever.
	bootCtx, bootCancel := context.WithTimeout(ctx, 1*time.Second)
	summary := st.RunRecovery(bootCtx)
	bootCancel()
	for id, outcome := range summary {
		logger.Printf("startup recovery: txn %s -> %s", id, outcome)
	}
	st.StartRecoveryLoop(ctx, 500*time.Millisecond)
	return serve(ctx, listen, st.Handler(), logger)
}

func parseParticipants(list string) (map[string]string, error) {
	out := map[string]string{}
	for _, item := range strings.Split(list, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		k, v, ok := strings.Cut(item, "=")
		if !ok || strings.TrimSpace(k) == "" || strings.TrimSpace(v) == "" {
			return nil, fmt.Errorf("bad participant entry %q, want name=URL", item)
		}
		out[strings.TrimSpace(k)] = strings.TrimSpace(v)
	}
	if len(out) == 0 {
		return nil, errors.New("no participants given")
	}
	return out, nil
}

func serve(ctx context.Context, listen string, h http.Handler, logger *log.Logger) error {
	srv := &http.Server{
		Addr:              listen,
		Handler:           h,
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		logger.Printf("listening on %s", listen)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Printf("http server: %v", err)
		}
	}()
	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	return srv.Shutdown(shutdownCtx)
}
