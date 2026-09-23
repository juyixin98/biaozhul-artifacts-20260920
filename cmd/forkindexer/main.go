// Command forkindexer runs the fork ledger indexer HTTP API and CLI tools.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"forkindexer/internal/domain"
	"forkindexer/internal/httpserver"
	"forkindexer/internal/pgstore"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "forkindexer:", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		usage()
		return errors.New("subcommand required")
	}
	switch args[0] {
	case "serve":
		return cmdServe(args[1:])
	case "migrate":
		return cmdMigrate(args[1:])
	case "ingest":
		return cmdIngest(args[1:])
	case "verify":
		return cmdVerify(args[1:])
	case "gen-examples":
		return cmdGenExamples(args[1:])
	case "-h", "--help", "help":
		usage()
		return nil
	default:
		usage()
		return fmt.Errorf("unknown subcommand %q", args[0])
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `forkindexer - fork ledger indexer

Usage:
  forkindexer serve      [-addr :8080] [-dsn $DATABASE_URL]
  forkindexer migrate    [-dsn $DATABASE_URL]
  forkindexer ingest     <file.ndjson> [-dsn ...]   (lines: block | envelope)
  forkindexer verify     [-dsn ...]
  forkindexer gen-examples <out-dir>

DATABASE_URL defaults to env $DATABASE_URL, then
postgres:///forkindexer?host=/var/run/postgresql
`)
}

func defaultDSN() string {
	if v := strings.TrimSpace(os.Getenv("DATABASE_URL")); v != "" {
		return v
	}
	return "postgres:///forkindexer?host=/var/run/postgresql"
}

func openStore(ctx context.Context, dsn string, migrate bool) (*pgstore.Store, error) {
	s, err := pgstore.New(ctx, dsn)
	if err != nil {
		return nil, err
	}
	if migrate {
		if err := s.Migrate(ctx); err != nil {
			s.Close()
			return nil, err
		}
	}
	return s, nil
}

func cmdServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	addr := fs.String("addr", envOr("ADDR", ":8080"), "listen address")
	dsn := fs.String("dsn", defaultDSN(), "PostgreSQL DSN")
	if err := fs.Parse(args); err != nil {
		return err
	}
	ctx, cancel := signalContext()
	defer cancel()

	store, err := openStore(ctx, *dsn, true)
	if err != nil {
		return err
	}
	defer store.Close()

	srv := &http.Server{
		Addr:              *addr,
		Handler:           httpserver.New(store).Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shCtx, c := context.WithTimeout(context.Background(), 5*time.Second)
		defer c()
		_ = srv.Shutdown(shCtx)
	}()
	fmt.Fprintf(os.Stderr, "forkindexer listening on %s\n", *addr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

func cmdMigrate(args []string) error {
	fs := flag.NewFlagSet("migrate", flag.ContinueOnError)
	dsn := fs.String("dsn", defaultDSN(), "PostgreSQL DSN")
	if err := fs.Parse(args); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	store, err := openStore(ctx, *dsn, true)
	if err != nil {
		return err
	}
	defer store.Close()
	fmt.Println("schema applied")
	return nil
}

func cmdVerify(args []string) error {
	fs := flag.NewFlagSet("verify", flag.ContinueOnError)
	dsn := fs.String("dsn", defaultDSN(), "PostgreSQL DSN")
	if err := fs.Parse(args); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	store, err := openStore(ctx, *dsn, false)
	if err != nil {
		return err
	}
	defer store.Close()
	rep, err := store.VerifyFromGenesis(ctx)
	if err != nil {
		return err
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(rep); err != nil {
		return err
	}
	if !rep.OK {
		return errors.New("verification FAILED: indexed state diverges from genesis rebuild")
	}
	fmt.Println("OK: indexed balances and chain equal a from-genesis rebuild")
	return nil
}

// cmdIngest reads NDJSON lines. Each line is either an envelope
// {"sequence":N,"block":{...}} or a bare block. Bare blocks do not move the
// cursor (ad-hoc seeding).
func cmdIngest(args []string) error {
	fs := flag.NewFlagSet("ingest", flag.ContinueOnError)
	dsn := fs.String("dsn", defaultDSN(), "PostgreSQL DSN")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 1 {
		return errors.New("ingest requires exactly one .ndjson file")
	}
	f, err := os.Open(fs.Arg(0))
	if err != nil {
		return err
	}
	defer f.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	store, err := openStore(ctx, *dsn, true)
	if err != nil {
		return err
	}
	defer store.Close()

	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 16<<20)
	var lineNo int
	var envelopes []domain.Envelope
	var bare []domain.Block
	flush := func() error {
		if len(envelopes) > 0 {
			if _, err := store.IngestBatch(ctx, envelopes); err != nil {
				return err
			}
			envelopes = nil
		}
		for i := range bare {
			if _, err := store.IngestEnvelope(ctx, &bare[i], nil); err != nil {
				return err
			}
		}
		bare = nil
		return nil
	}
	for sc.Scan() {
		lineNo++
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "//") || strings.HasPrefix(line, "#") {
			continue
		}
		var probe map[string]json.RawMessage
		if err := json.Unmarshal([]byte(line), &probe); err != nil {
			return fmt.Errorf("line %d: %w", lineNo, err)
		}
		if _, ok := probe["block"]; ok {
			var env domain.Envelope
			if err := json.Unmarshal([]byte(line), &env); err != nil {
				return fmt.Errorf("line %d: %w", lineNo, err)
			}
			if err := domain.Normalize(&env.Block); err != nil {
				return fmt.Errorf("line %d: %w", lineNo, err)
			}
			envelopes = append(envelopes, env)
			if len(envelopes) >= 64 {
				if err := flush(); err != nil {
					return fmt.Errorf("line %d: %w", lineNo, err)
				}
			}
		} else {
			var b domain.Block
			if err := json.Unmarshal([]byte(line), &b); err != nil {
				return fmt.Errorf("line %d: %w", lineNo, err)
			}
			if err := domain.Normalize(&b); err != nil {
				return fmt.Errorf("line %d: %w", lineNo, err)
			}
			bare = append(bare, b)
		}
	}
	if err := sc.Err(); err != nil {
		return err
	}
	if err := flush(); err != nil {
		return err
	}
	fmt.Printf("ingested %d lines\n", lineNo)
	return nil
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func signalContext() (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(context.Background())
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-ch
		cancel()
	}()
	return ctx, cancel
}
