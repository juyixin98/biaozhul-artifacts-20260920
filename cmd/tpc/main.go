// Command tpc runs a two-phase-commit node: either a coordinator or a
// participant. See README.md for the protocol, crash points and examples.
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"

	"tpc/internal/apprun"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	fs := flag.NewFlagSet(os.Args[1], flag.ExitOnError)
	listen := fs.String("listen", "127.0.0.1:0", "address to listen on")
	datadir := fs.String("datadir", "", "directory for the durable WAL (required)")
	name := fs.String("name", "", "node name used in logs and responses")
	var participants string
	if os.Args[1] == "coordinator" {
		fs.StringVar(&participants, "participants", "", "comma-separated name=URL list (required for coordinator)")
	}
	_ = fs.Parse(os.Args[2:])

	if *datadir == "" || *name == "" {
		usage()
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	var err error
	switch os.Args[1] {
	case "participant":
		err = apprun.RunParticipant(ctx, *name, *listen, *datadir)
	case "coordinator":
		if participants == "" {
			usage()
		}
		err = apprun.RunCoordinator(ctx, *name, *listen, *datadir, participants)
	default:
		usage()
	}
	if err != nil {
		log.Fatal(err)
	}
}

func usage() {
	log.Printf("usage:\n" +
		"  tpc participant -name p1 -listen 127.0.0.1:9101 -datadir data/p1\n" +
		"  tpc coordinator -name c1 -listen 127.0.0.1:9000 -datadir data/c1 \\\n" +
		"      -participants p1=http://127.0.0.1:9101,p2=http://127.0.0.1:9102")
	os.Exit(2)
}
