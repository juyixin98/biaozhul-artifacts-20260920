// Command buildctl is a thin JSON client for the build service.
//
//	buildctl fixtures
//	buildctl start <fixture>
//	buildctl wait <job-id> [timeout]
//	buildctl status <job-id>
//	buildctl list
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"
)

func main() {
	url := flag.String("url", envOr("BUILD_URL", "http://127.0.0.1:8081"), "build service base URL")
	flag.Parse()
	args := flag.Args()
	if len(args) == 0 {
		usage()
		os.Exit(2)
	}
	ctx := context.Background()
	var err error
	switch args[0] {
	case "fixtures":
		err = getJSON(ctx, *url+"/v1/fixtures")
	case "start":
		if len(args) != 2 {
			err = fmt.Errorf("start requires <fixture>")
			break
		}
		err = startBuild(ctx, *url, args[1])
	case "wait":
		if len(args) < 2 {
			err = fmt.Errorf("wait requires <job-id> [timeout]")
			break
		}
		timeout := "2m"
		if len(args) == 3 {
			timeout = args[2]
		}
		if _, perr := time.ParseDuration(timeout); perr != nil {
			err = fmt.Errorf("bad timeout %q: %w", timeout, perr)
			break
		}
		err = getJSON(ctx, fmt.Sprintf("%s/v1/builds/%s?wait=%s", *url, args[1], timeout))
	case "status":
		if len(args) != 2 {
			err = fmt.Errorf("status requires <job-id>")
			break
		}
		err = getJSON(ctx, *url+"/v1/builds/"+args[1])
	case "list":
		err = getJSON(ctx, *url+"/v1/builds")
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage: buildctl [-url URL] <command> [args]
  fixtures                 list allowed fixtures
  start <fixture>          start a build, prints job JSON
  wait <job-id> [timeout]  wait for terminal state (default 2m)
  status <job-id>
  list`)
}

func getJSON(ctx context.Context, u string) error {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	return doAndPrint(req)
}

func startBuild(ctx context.Context, base, fixture string) error {
	body, _ := json.Marshal(map[string]string{"fixture": fixture})
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, base+"/v1/builds", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	return doAndPrint(req)
}

func doAndPrint(req *http.Request) error {
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var pretty bytes.Buffer
	if jerr := json.Indent(&pretty, raw, "", "  "); jerr == nil {
		raw = pretty.Bytes()
	}
	fmt.Println(string(raw))
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return nil
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
