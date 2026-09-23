// Command gwctl is the gateway admin CLI: hot-swap the active mapping
// version and inspect runtime stats.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	gatewayv1 "github.com/p079/telegw/gen/gateway/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func main() {
	addr := flag.String("addr", envOr("GATEWAY_ADDR", "localhost:50051"), "gateway address")
	flag.Parse()
	args := flag.Args()
	if len(args) < 1 {
		fmt.Fprintln(os.Stderr, "usage: gwctl [-addr HOST:PORT] <reload [VERSION]|stats>")
		os.Exit(2)
	}

	conn, err := grpc.NewClient(*addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		fmt.Fprintf(os.Stderr, "dial: %v\n", err)
		os.Exit(1)
	}
	defer conn.Close()
	client := gatewayv1.NewGatewayClient(conn)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	switch args[0] {
	case "reload":
		version := ""
		if len(args) > 1 {
			version = args[1]
		}
		resp, err := client.ReloadMapping(ctx, &gatewayv1.ReloadMappingRequest{MappingVersion: version})
		if err != nil {
			fmt.Fprintf(os.Stderr, "reload: %v\n", err)
			os.Exit(1)
		}
		fmt.Printf("active mapping: %s (available: %v)\n", resp.ActiveMappingVersion, resp.AvailableVersions)
	case "stats":
		resp, err := client.GetStats(ctx, &gatewayv1.GetStatsRequest{})
		if err != nil {
			fmt.Fprintf(os.Stderr, "stats: %v\n", err)
			os.Exit(1)
		}
		fmt.Printf("converted_ok=%d conversion_errors=%d in_flight=%d unknown_fields_dropped=%d audit_failures=%d\n",
			resp.ConvertedOk, resp.ConversionErrors, resp.InFlight, resp.UnknownFieldsDropped, resp.AuditFailures)
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n", args[0])
		os.Exit(2)
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
