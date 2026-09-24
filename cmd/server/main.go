// Command shardmig-server runs the shard migration cutover simulator as three
// HTTP services (controller + node A + node B) on loopback.
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"

	"shardmig/api"
	"shardmig/core"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "controller listen address (nodes use ephemeral ports)")
	flag.Parse()

	sys, err := api.New(core.NewCluster(), *addr)
	if err != nil {
		log.Fatalf("start simulator: %v", err)
	}
	defer sys.Close()

	fmt.Fprintf(os.Stderr, "shard migration simulator started\n")
	fmt.Fprintf(os.Stderr, "  controller : %s\n", sys.ControllerURL())
	fmt.Fprintf(os.Stderr, "  node A     : %s\n", sys.NodeURL(core.NodeA))
	fmt.Fprintf(os.Stderr, "  node B     : %s\n", sys.NodeURL(core.NodeB))
	fmt.Fprintf(os.Stderr, "press Ctrl-C to stop\n")

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	fmt.Fprintln(os.Stderr, "shutting down")
}
