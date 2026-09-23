// Command genscenarios writes the built-in scenario library as JSON files so
// they can be inspected and POSTed to /scenarios/run by hand.
//
//	go run ./cmd/genscenarios -out scenarios
package main

import (
	"encoding/json"
	"flag"
	"log"
	"os"
	"path/filepath"

	"raftlab/verify"
)

func main() {
	out := flag.String("out", "scenarios", "output directory")
	flag.Parse()
	if err := os.MkdirAll(*out, 0o755); err != nil {
		log.Fatal(err)
	}

	write := func(sc verify.Scenario) {
		path := filepath.Join(*out, sc.Name+".json")
		b, err := json.MarshalIndent(sc, "", "  ")
		if err != nil {
			log.Fatal(err)
		}
		b = append(b, '\n')
		if err := os.WriteFile(path, b, 0o644); err != nil {
			log.Fatal(err)
		}
		log.Printf("wrote %s", path)
	}

	for _, variant := range []string{"standard", "naive"} {
		for _, sc := range verify.LibraryScenarios(variant) {
			if sc.Name == "five-node-chaos" {
				sc.Nodes = 5
			}
			sc.Name = variant + "-" + sc.Name
			write(sc)
		}
	}
	for _, sc := range verify.CounterexampleScenarios() {
		write(sc)
		write(verify.StandardCounterpart(sc))
	}
}
