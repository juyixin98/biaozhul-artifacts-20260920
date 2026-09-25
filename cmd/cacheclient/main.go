// Command cacheclient is a small CLI around the client package: upload,
// download (digest-verified), run fixture builds, and inspect the cache.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"

	"localcache/api"
	"localcache/client"
)

func usage() {
	fmt.Fprintf(os.Stderr, `usage: cacheclient -addr <base-url> <command> [args]

commands:
  put <file>                      upload a file, print its digest
  get <digest> <out-file>         download and digest-verify an object
  has <digest>                    check whether an object exists
  build [-in name=digest]... [-out name]... [-timeout-ms N] -- <argv>...
                                  run an explicit fixture command (cached)
  stats                           print cache statistics
  fsck                            run a cache consistency scan
`)
	os.Exit(2)
}

type strList []string

func (s *strList) String() string { return strings.Join(*s, ",") }
func (s *strList) Set(v string) error {
	*s = append(*s, v)
	return nil
}

func main() {
	addr := flag.String("addr", "http://127.0.0.1:8080", "cache server base URL")
	flag.Parse()
	args := flag.Args()
	if len(args) == 0 {
		usage()
	}
	c := client.New(*addr)
	ctx := context.Background()

	switch args[0] {
	case "put":
		if len(args) != 2 {
			usage()
		}
		data, err := os.ReadFile(args[1])
		must(err)
		digest, err := c.PutBytes(ctx, data)
		must(err)
		fmt.Println(digest)

	case "get":
		if len(args) != 3 {
			usage()
		}
		data, err := c.Get(ctx, args[1])
		must(err)
		must(os.WriteFile(args[2], data, 0o644))
		fmt.Fprintf(os.Stderr, "wrote %d bytes to %s (digest verified)\n", len(data), args[2])

	case "has":
		if len(args) != 2 {
			usage()
		}
		ok, err := c.Has(ctx, args[1])
		must(err)
		fmt.Println(ok)

	case "build":
		fs := flag.NewFlagSet("build", flag.ExitOnError)
		var ins, outs strList
		var timeoutMs int
		fs.Var(&ins, "in", "input mapping name=digest (repeatable)")
		fs.Var(&outs, "out", "declared output path (repeatable)")
		fs.IntVar(&timeoutMs, "timeout-ms", 0, "per-command timeout in milliseconds")
		// Everything after "--" is the explicit command to run.
		sep := len(args)
		for i, a := range args {
			if a == "--" {
				sep = i
				break
			}
		}
		must(fs.Parse(args[1:sep]))
		argv := args[min(sep+1, len(args)):]
		req := &api.BuildRequest{Argv: argv, Outputs: outs, TimeoutMs: timeoutMs}
		if len(ins) > 0 {
			req.Inputs = map[string]string{}
			for _, in := range ins {
				name, digest, found := strings.Cut(in, "=")
				if !found {
					must(fmt.Errorf("bad -in %q, want name=digest", in))
				}
				req.Inputs[name] = digest
			}
		}
		res, err := c.Build(ctx, req)
		must(err)
		printJSON(res)

	case "stats":
		st, err := c.Stats(ctx)
		must(err)
		printJSON(st)

	case "fsck":
		rep, err := c.Fsck(ctx)
		must(err)
		printJSON(rep)
		if !rep.OK {
			os.Exit(1)
		}

	default:
		usage()
	}
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func printJSON(v any) {
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	must(enc.Encode(v))
}
