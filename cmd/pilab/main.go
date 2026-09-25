// pilab is the command-line entry point for the priority-inheritance lab.
//
// Usage:
//
//	pilab serve [-addr :8080]                start the HTTP API
//	pilab scenario <id>                     run a built-in scenario, print JSON
//	pilab timeline <id>                     print a compact text timeline
//	pilab list                              list built-in scenarios
//	pilab compare                           run inversion with/without PIP
//	pilab run [-] [file.json]               run a config from file or stdin
//
// All output is JSON on stdout so results can be redirected to files.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"

	"pilab/httpapi"
	"pilab/scenario"
	"pilab/scheduler"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "pilab:", err)
		os.Exit(1)
	}
}

func printJSON(v any) error {
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}

func run(args []string) error {
	if len(args) == 0 {
		return errors.New("subcommand required: serve | scenario | list | compare | run")
	}
	switch args[0] {
	case "serve":
		return cmdServe(args[1:])
	case "scenario":
		return cmdScenario(args[1:])
	case "timeline":
		return cmdTimeline(args[1:])
	case "list":
		return cmdList()
	case "compare":
		return cmdCompare()
	case "run":
		return cmdRun(args[1:])
	case "-h", "--help", "help":
		fmt.Println(usage)
		return nil
	default:
		return fmt.Errorf("unknown subcommand %q", args[0])
	}
}

const usage = `pilab — priority-inheritance simulation lab

  pilab serve [-addr :8080]
  pilab list
  pilab scenario <id>
  pilab timeline <id>
  pilab compare
  pilab run [file.json | -]   ('-' or no file reads stdin)`

func cmdServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	addr := fs.String("addr", ":8080", "listen address")
	if err := fs.Parse(args); err != nil {
		return err
	}
	srv := httpapi.NewServer()
	fmt.Fprintln(os.Stderr, "pilab: listening on", *addr)
	fmt.Fprintln(os.Stderr, "endpoints: GET /healthz GET /api/scenarios POST /api/scenarios/<id>")
	fmt.Fprintln(os.Stderr, "           POST /api/simulate POST /api/simulate/stream GET /api/compare/inversion")
	return http.ListenAndServe(*addr, srv.Handler())
}

func cmdList() error {
	return printJSON(scenario.All())
}

func cmdScenario(args []string) error {
	if len(args) != 1 {
		return errors.New("usage: pilab scenario <id> (see `pilab list`)")
	}
	sc, ok := scenario.Get(scenario.ID(args[0]))
	if !ok {
		return fmt.Errorf("unknown scenario %q", args[0])
	}
	rep, err := scenario.Run(sc)
	if err != nil {
		return err
	}
	return printJSON(rep)
}

func cmdTimeline(args []string) error {
	if len(args) != 1 {
		return errors.New("usage: pilab timeline <id> (see `pilab list`)")
	}
	sc, ok := scenario.Get(scenario.ID(args[0]))
	if !ok {
		return fmt.Errorf("unknown scenario %q", args[0])
	}
	rep, err := scenario.Run(sc)
	if err != nil {
		return err
	}
	_, err = fmt.Println(scheduler.TimelineText(rep))
	return err
}

func cmdCompare() error {
	cmp, err := scenario.CompareInversion()
	if err != nil {
		return err
	}
	return printJSON(cmp)
}

func cmdRun(args []string) error {
	var in io.Reader = os.Stdin
	if len(args) == 1 && args[0] != "-" {
		f, err := os.Open(args[0])
		if err != nil {
			return err
		}
		defer f.Close()
		in = f
	}
	var cfg scheduler.Config
	dec := json.NewDecoder(in)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&cfg); err != nil {
		return fmt.Errorf("decode config: %w", err)
	}
	s, err := scheduler.New(cfg)
	if err != nil {
		return err
	}
	return printJSON(s.Run())
}
