// cachectl is a small CLI client for the build cache server.
//
//	cachectl [-addr URL] build task.json   submit a build task
//	cachectl [-addr URL] get <key>         show one cache entry
//	cachectl [-addr URL] audit <key>       show the inputs bound into a key
//	cachectl [-addr URL] list              list all entries
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
)

func main() {
	addr := flag.String("addr", envOr("CACHE_ADDR", "http://127.0.0.1:8080"), "server base URL")
	flag.Parse()
	args := flag.Args()
	if len(args) < 1 {
		fmt.Fprintln(os.Stderr, "usage: cachectl [-addr URL] <build task.json | get KEY | audit KEY | list>")
		os.Exit(2)
	}

	var code int
	var err error
	switch args[0] {
	case "build":
		if len(args) != 2 {
			fmt.Fprintln(os.Stderr, "usage: cachectl build task.json")
			os.Exit(2)
		}
		code, err = doBuild(*addr, args[1])
	case "get":
		code, err = doGet(*addr, "/v1/builds/", args)
	case "audit":
		code, err = doGet(*addr, "/v1/audit/", args)
	case "list":
		code, err = doGet(*addr, "/v1/entries", args)
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n", args[0])
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	if code >= 400 {
		os.Exit(1)
	}
}

func doBuild(addr, taskFile string) (int, error) {
	body, err := os.ReadFile(taskFile)
	if err != nil {
		return 0, err
	}
	resp, err := http.Post(addr+"/v1/builds", "application/json", bytes.NewReader(body))
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	return resp.StatusCode, printPretty(resp.Body)
}

func doGet(addr, prefix string, args []string) (int, error) {
	url := addr + prefix
	if len(args) == 2 {
		url += args[1]
	} else if len(args) != 1 {
		return 0, fmt.Errorf("usage: cachectl %s [key]", args[0])
	}
	resp, err := http.Get(url)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	return resp.StatusCode, printPretty(resp.Body)
}

func printPretty(r io.Reader) error {
	var v any
	dec := json.NewDecoder(r)
	if err := dec.Decode(&v); err != nil {
		return err
	}
	out, _ := json.MarshalIndent(v, "", "  ")
	fmt.Println(string(out))
	return nil
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
