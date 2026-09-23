// Command respc is a tiny example client. It encodes its arguments as one
// pipelined RESP2 request (one array per command line argument pair), sends
// it to a running respd over HTTP, and prints every reply.
//
// It doubles as a payload generator:
//
//	# print the request body to stdout
//	respc -encode SET k 'hello' GET k
//
//	# round-trip against the server
//	respc -url http://127.0.0.1:8080/resp PING SET k v GET k
//
// Commands are separated by the literal "--" so arguments containing spaces
// or dashes stay unambiguous, e.g.
//
//	respc SET greet "a b" -- GET greet
package main

import (
	"bytes"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"

	"respd/internal/resp"
)

func main() {
	url := flag.String("url", "http://127.0.0.1:8080/resp", "respd endpoint")
	encodeOnly := flag.Bool("encode", false, "print the RESP request body and exit")
	flag.Parse()

	groups := splitGroups(flag.Args())
	if len(groups) == 0 {
		fmt.Fprintln(os.Stderr, "usage: respc [--url URL] CMD arg... [-- CMD arg...]")
		os.Exit(2)
	}

	var body bytes.Buffer
	w := resp.NewWriter(&body)
	for _, g := range groups {
		els := make([]*resp.Value, len(g))
		for i, a := range g {
			els[i] = resp.BulkVal([]byte(a))
		}
		if err := w.WriteValue(&resp.Value{Type: resp.Array, Array: els}); err != nil {
			fail(err)
		}
	}

	if *encodeOnly {
		os.Stdout.Write(body.Bytes())
		return
	}

	httpResp, err := http.Post(*url, "application/octet-stream", &body)
	if err != nil {
		fail(err)
	}
	defer httpResp.Body.Close()
	data, err := io.ReadAll(httpResp.Body)
	if err != nil {
		fail(err)
	}
	fmt.Printf("HTTP %d\n", httpResp.StatusCode)
	if err := printReplies(data); err != nil {
		fail(err)
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "respc:", err)
	os.Exit(1)
}

func splitGroups(args []string) [][]string {
	var groups [][]string
	var cur []string
	for _, a := range args {
		if a == "--" {
			if len(cur) > 0 {
				groups = append(groups, cur)
				cur = nil
			}
			continue
		}
		cur = append(cur, a)
	}
	if len(cur) > 0 {
		groups = append(groups, cur)
	}
	return groups
}

func printReplies(data []byte) error {
	r := resp.NewReader(bytes.NewReader(data))
	i := 0
	for {
		v, err := r.ReadMessage()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		fmt.Printf("[%d] %s\n", i, format(v))
		i++
	}
}

func format(v *resp.Value) string {
	switch v.Type {
	case resp.SimpleString:
		return "+" + v.Text
	case resp.Error:
		return "-" + v.Text
	case resp.Integer:
		return ":" + strconv.FormatInt(v.Int, 10)
	case resp.NullBulk:
		return "$-1  (null)"
	case resp.NullArray:
		return "*-1  (null)"
	case resp.BulkString:
		return fmt.Sprintf("$%d %q", len(v.Str), string(v.Str))
	case resp.Array:
		parts := make([]string, len(v.Array))
		for i, c := range v.Array {
			parts[i] = format(c)
		}
		return "*" + strconv.Itoa(len(v.Array)) + " [" + strings.Join(parts, ", ") + "]"
	}
	return "?"
}
