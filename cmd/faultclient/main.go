// Command faultclient drives the fault-injection admin endpoint of the
// local server, letting tests and operators inject latency and forced
// failures into the in-process fake backend.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"
)

func main() {
	addr := flag.String("addr", "http://127.0.0.1:8080", "server base URL")
	latency := flag.Duration("latency", 0, "latency injected into every compute call")
	failNext := flag.Int("fail-next", 0, "force the next N compute calls to fail")
	show := flag.Bool("show", false, "only show current fault settings")
	flag.Parse()

	if *show {
		resp, err := http.Get(*addr + "/admin/faults")
		must(err)
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		fmt.Println(string(body))
		return
	}

	payload, err := json.Marshal(map[string]any{
		"latency":   int64(*latency / time.Nanosecond),
		"fail_next": *failNext,
	})
	must(err)
	req, err := http.NewRequest(http.MethodPut, *addr+"/admin/faults", bytes.NewReader(payload))
	must(err)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	must(err)
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		fmt.Fprintf(os.Stderr, "server rejected fault settings: %s: %s\n", resp.Status, body)
		os.Exit(1)
	}
	fmt.Printf("faults set: %s\n", body)
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "faultclient:", err)
		os.Exit(1)
	}
}
