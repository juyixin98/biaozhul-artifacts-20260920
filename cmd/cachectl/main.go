// Command cachectl is a small operator CLI for the model artifact cache.
//
//	cachectl put <file>                 hash + upload, prints digest
//	cachectl get <digest> <out-file>    verified, resumable download
//	cachectl cat <digest>               stream verified object to stdout
//	cachectl stat <digest>              size / presence
//	cachectl verify <digest>            rehash server-side (quarantines bad)
//	cachectl delete <digest>
//	cachectl list
//	cachectl stats
//	cachectl doctor                     list objects, verify each, report
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"time"

	"modelcache/client"
	"modelcache/digest"
)

func main() {
	url := flag.String("url", envOr("CACHE_URL", "http://127.0.0.1:8080"), "cache base URL")
	flag.Parse()
	args := flag.Args()
	if len(args) == 0 {
		usage()
		os.Exit(2)
	}
	cl := client.New(client.Options{BaseURL: *url, UserAgent: "cachectl/1.0"})
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()

	cmd, rest := args[0], args[1:]
	var err error
	switch cmd {
	case "put":
		err = put(cl, ctx, rest)
	case "get":
		err = get(cl, ctx, rest)
	case "cat":
		err = cat(cl, ctx, rest)
	case "stat":
		err = stat(cl, ctx, rest)
	case "verify":
		err = verify(cl, ctx, rest)
	case "delete", "rm":
		err = del(cl, ctx, rest)
	case "list", "ls":
		err = list(cl, ctx)
	case "stats":
		err = stats(cl, ctx)
	case "doctor":
		err = doctor(cl, ctx)
	case "-h", "--help", "help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n", cmd)
		usage()
		os.Exit(2)
	}
	if err != nil {
		var he *client.HTTPError
		if errors.As(err, &he) {
			fmt.Fprintf(os.Stderr, "error: HTTP %d %s\n", he.StatusCode, he.Message)
		} else {
			fmt.Fprintf(os.Stderr, "error: %v\n", err)
		}
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage: cachectl [-url URL] <command> [args]
commands:
  put <file>                 hash and upload a file
  get <digest> <out-file>    verified resumable download
  cat <digest>               stream verified object to stdout
  stat <digest>              show object size
  verify <digest>            server-side rehash (quarantines corrupt blob)
  delete <digest>            evict object
  list                       list objects
  stats                      show cache statistics
  doctor                     verify every object, report bad cache`)
}

func put(cl *client.Client, ctx context.Context, args []string) error {
	if len(args) != 1 {
		return fmt.Errorf("put requires <file>")
	}
	pr, err := cl.PutFile(ctx, args[0])
	if err != nil {
		return err
	}
	fmt.Printf("digest:  %s\nsize:    %d\nexisted: %v\n", pr.Digest, pr.Size, pr.Existed)
	return nil
}

func get(cl *client.Client, ctx context.Context, args []string) error {
	if len(args) != 2 {
		return fmt.Errorf("get requires <digest> <out-file>")
	}
	dgst, err := digest.Parse(args[0])
	if err != nil {
		return err
	}
	n, err := cl.DownloadToFile(ctx, dgst, args[1])
	if err != nil {
		return err
	}
	fmt.Printf("downloaded %d bytes -> %s (verified %s)\n", n, args[1], dgst)
	return nil
}

func cat(cl *client.Client, ctx context.Context, args []string) error {
	if len(args) != 1 {
		return fmt.Errorf("cat requires <digest>")
	}
	dgst, err := digest.Parse(args[0])
	if err != nil {
		return err
	}
	rc, _, err := cl.Get(ctx, dgst)
	if err != nil {
		return err
	}
	defer rc.Close()
	_, err = io.Copy(os.Stdout, rc)
	return err
}

func stat(cl *client.Client, ctx context.Context, args []string) error {
	if len(args) != 1 {
		return fmt.Errorf("stat requires <digest>")
	}
	dgst, err := digest.Parse(args[0])
	if err != nil {
		return err
	}
	size, err := cl.Stat(ctx, dgst)
	if err != nil {
		return err
	}
	fmt.Printf("%s  %d bytes\n", dgst, size)
	return nil
}

func verify(cl *client.Client, ctx context.Context, args []string) error {
	if len(args) != 1 {
		return fmt.Errorf("verify requires <digest>")
	}
	dgst, err := digest.Parse(args[0])
	if err != nil {
		return err
	}
	size, err := cl.Verify(ctx, dgst)
	if err != nil {
		return err
	}
	fmt.Printf("OK %s (%d bytes)\n", dgst, size)
	return nil
}

func del(cl *client.Client, ctx context.Context, args []string) error {
	if len(args) != 1 {
		return fmt.Errorf("delete requires <digest>")
	}
	dgst, err := digest.Parse(args[0])
	if err != nil {
		return err
	}
	if err := cl.Delete(ctx, dgst); err != nil {
		return err
	}
	fmt.Printf("deleted %s\n", dgst)
	return nil
}

func list(cl *client.Client, ctx context.Context) error {
	blobs, err := cl.List(ctx)
	if err != nil {
		return err
	}
	for _, b := range blobs {
		fmt.Printf("%s  %10d\n", b.Digest, b.Size)
	}
	if len(blobs) == 0 {
		fmt.Println("(cache is empty)")
	}
	return nil
}

func stats(cl *client.Client, ctx context.Context) error {
	st, err := cl.Stats(ctx)
	if err != nil {
		return err
	}
	fmt.Printf("objects:       %d\n", st.Objects)
	fmt.Printf("total bytes:   %d\n", st.TotalBytes)
	fmt.Printf("max size:      %d\n", st.MaxObjectSize)
	fmt.Printf("temp files:    %d\n", st.TempFiles)
	fmt.Printf("quarantined:   %d\n", st.Quarantined)
	return nil
}

func doctor(cl *client.Client, ctx context.Context) error {
	blobs, err := cl.List(ctx)
	if err != nil {
		return err
	}
	bad := 0
	fmt.Printf("doctor: verifying %d object(s)...\n", len(blobs))
	for _, b := range blobs {
		size, verr := cl.Verify(ctx, b.Digest)
		if verr != nil {
			bad++
			fmt.Printf("  BAD  %s (%d bytes): %v\n", b.Digest, size, verr)
			continue
		}
		fmt.Printf("  ok   %s (%d bytes)\n", b.Digest, size)
	}
	if bad > 0 {
		return fmt.Errorf("%d corrupt object(s) found (moved to quarantine)", bad)
	}
	fmt.Println("doctor: all objects verified")
	return nil
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
