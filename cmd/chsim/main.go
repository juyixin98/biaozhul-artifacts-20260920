// Command chsim runs the consistent-hash migration simulator.
//
//	chsim -scenario file.json          run one scenario, print result JSON
//	chsim -listen :8080                serve POST /run and GET /healthz
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"

	"chsim/internal/api"
	"chsim/internal/sim"
)

func main() {
	listen := flag.String("listen", "", "HTTP listen address, e.g. :8080")
	scenario := flag.String("scenario", "", "run this scenario JSON file once and print the result")
	compact := flag.Bool("compact", false, "print result as a single JSON line")
	flag.Parse()

	if *scenario != "" {
		if err := runOnce(*scenario, !*compact); err != nil {
			log.Fatalf("run failed: %v", err)
		}
		return
	}

	addr := *listen
	if addr == "" {
		addr = ":8080"
	}
	log.Printf("chsim listening on %s (POST /run, GET /healthz)", addr)
	log.Fatal(http.ListenAndServe(addr, api.Handler()))
}

func runOnce(path string, pretty bool) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var sc sim.Scenario
	if err := json.Unmarshal(data, &sc); err != nil {
		return fmt.Errorf("invalid scenario JSON: %w", err)
	}
	res, err := sim.Run(sc)
	if err != nil {
		return err
	}
	var out []byte
	if pretty {
		out, err = json.MarshalIndent(res, "", "  ")
	} else {
		out, err = json.Marshal(res)
	}
	if err != nil {
		return err
	}
	fmt.Println(string(out))
	if !res.Verification.Pass {
		return fmt.Errorf("verification FAILED")
	}
	return nil
}
