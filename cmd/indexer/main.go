// Command indexer drives the ledger index from a local NDJSON block stream.
//
//	indexer ingest-file [--exit-after N]   stream blocks from a file, durably
//	                                        committing the byte offset cursor
//	indexer reset                           wipe all indexed state
//	indexer verify                          recompute from genesis and compare
//
// Crash/restart semantics: ingest-file always rescans the stream from the
// beginning. Blocks already committed are skipped content-addressed by hash
// (duplicate delivery never double-counts), so re-reading them is free and
// safe. The durable stream_offset is a high-water mark committed in the same
// transaction as blocks + balances, useful as an external cursor; an
// interrupted batch is simply replayed.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"os"

	"forkindexer/internal/indexer"
	"forkindexer/internal/model"
	"forkindexer/internal/store"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	cmd := os.Args[1]
	args := os.Args[2:]

	ctx := context.Background()
	dsn := env("DATABASE_URL", "postgres://localhost:5432/forkindexer?sslmode=disable")
	pool, err := store.Open(ctx, dsn)
	must(err)
	defer pool.Close()
	must(store.Migrate(ctx, pool))
	ix := indexer.New(pool)

	switch cmd {
	case "ingest-file":
		fs := flag.NewFlagSet("ingest-file", flag.ExitOnError)
		path := fs.String("file", "", "path to NDJSON block stream")
		batch := fs.Int("batch-size", 256, "blocks per committed transaction")
		exitAfter := fs.Int("exit-after", 0, "crash-test hook: exit(0) as soon as this process has committed N new blocks")
		exitBeforeBatch := fs.Int("exit-before-batch", 0, "crash-test hook: exit(0) just before committing batch N of this process (before the batch commits)")
		_ = fs.Parse(args)
		if *path == "" {
			fs.Usage()
			os.Exit(2)
		}
		os.Exit(runIngestFile(ctx, ix, *path, *batch, *exitAfter, *exitBeforeBatch))
	case "reset":
		must(store.Reset(ctx, pool))
		log.Print("state reset")
	case "state":
		st, err := ix.State(ctx)
		must(err)
		out, _ := json.MarshalIndent(st, "", "  ")
		fmt.Println(string(out))
	case "verify":
		rep, err := ix.VerifyAgainstRebuild(ctx)
		must(err)
		out, _ := json.MarshalIndent(rep, "", "  ")
		fmt.Println(string(out))
		if !rep.Match {
			os.Exit(1)
		}
	default:
		usage()
		os.Exit(2)
	}
}

func runIngestFile(ctx context.Context, ix *indexer.Indexer, path string, batchSize, exitAfter, exitBeforeBatch int) int {
	f, err := os.Open(path)
	if err != nil {
		log.Fatalf("open: %v", err)
	}
	defer f.Close()

	st, err := ix.State(ctx)
	if err != nil {
		log.Fatalf("state: %v", err)
	}
	if st.StreamOffset > 0 {
		log.Printf("resuming: rescanning stream from start; committed blocks dedup by hash (cursor=%d)", st.StreamOffset)
	}

	var (
		blocks     []model.Block
		lineno     int
		totalAdded int
		batchNo    int
	)

	commit := func(lineEndOffset int64) {
		if len(blocks) == 0 {
			return
		}
		batchNo++
		if exitBeforeBatch > 0 && batchNo == exitBeforeBatch {
			log.Printf("exit-before-batch %d: crashing BEFORE commit (nothing from this batch is durable)", batchNo)
			os.Exit(0)
		}
		off := lineEndOffset
		res, err := ix.Ingest(ctx, blocks, &off)
		if err != nil {
			log.Fatalf("ingest failed at line %d (offset %d): %v", lineno, off, err)
		}
		totalAdded += res.Accepted
		log.Printf("committed batch %d: +%d new, %d duplicate, head=%s height=%d offset=%d",
			batchNo, res.Accepted, res.Duplicates, res.HeadHash, res.HeadHeight, res.StreamOffset)
		if res.Reorg != nil {
			log.Printf("REORG %s -> %s (undone=%d applied=%d)",
				res.Reorg.FromHead, res.Reorg.ToHead, len(res.Reorg.Undone), len(res.Reorg.Applied))
		}
		blocks = blocks[:0]
		if exitAfter > 0 && totalAdded >= exitAfter {
			log.Printf("exit-after %d reached: exiting now to simulate a crash", exitAfter)
			os.Exit(0)
		}
	}

	reader := bufio.NewReader(f)
	var pos int64
	for {
		line, err := reader.ReadBytes('\n')
		n := int64(len(line))
		lineStart := pos
		pos += n
		lineno++

		trimmed := bytes.TrimSpace(line)
		if len(trimmed) > 0 {
			var b model.Block
			if jerr := json.Unmarshal(trimmed, &b); jerr != nil {
				log.Fatalf("line %d: invalid block JSON: %v", lineno, jerr)
			}
			blocks = append(blocks, b)
			if len(blocks) >= batchSize {
				commit(pos)
			}
		}

		if err == io.EOF {
			// Only advance past complete lines (pos already sits after the
			// final newline); a trailing partial line is re-read next run.
			if len(trimmed) > 0 && line[len(line)-1] != '\n' {
				pos = lineStart // do not commit a partial last line
			}
			// A full final batch was already committed when the threshold
			// fired; commit() no-ops now because blocks was reset.
			commit(pos)
			break
		}
		if err != nil {
			log.Fatalf("read: %v", err)
		}
	}

	st2, _ := ix.State(ctx)
	log.Printf("done: %d new blocks this run, ingestSeq=%d offset=%d head=%s",
		totalAdded, st2.IngestSeq, st2.StreamOffset, st2.HeadHash)
	return 0
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: indexer <ingest-file|reset|verify> [flags]")
}

func must(err error) {
	if err != nil {
		log.Fatal(err)
	}
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
