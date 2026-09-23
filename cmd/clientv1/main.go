// Command clientv1 is the legacy-schema (v1) client: it streams v1
// readings from a JSONL file to the gateway and prints the converted
// envelopes as JSON lines.
package main

import (
	"bufio"
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"time"

	gatewayv1 "github.com/p079/telegw/gen/gateway/v1"
	telemetryv1 "github.com/p079/telegw/gen/telemetry/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/encoding/protojson"
)

func main() {
	addr := flag.String("addr", envOr("GATEWAY_ADDR", "localhost:50051"), "gateway address")
	input := flag.String("input", "examples/readings_v1.jsonl", "JSONL file of telemetry.v1.Reading (\"-\" for stdin)")
	timeout := flag.Duration("timeout", 30*time.Second, "overall RPC timeout")
	flag.Parse()

	f := os.Stdin
	if *input != "-" {
		var err error
		f, err = os.Open(*input)
		if err != nil {
			fmt.Fprintf(os.Stderr, "open input: %v\n", err)
			os.Exit(1)
		}
		defer f.Close()
	}

	conn, err := grpc.NewClient(*addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		fmt.Fprintf(os.Stderr, "dial: %v\n", err)
		os.Exit(1)
	}
	defer conn.Close()

	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()

	stream, err := gatewayv1.NewGatewayClient(conn).ConvertV1ToV2(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open stream: %v\n", err)
		os.Exit(1)
	}

	// Receiver: print envelopes as they arrive.
	recvDone := make(chan error, 1)
	go func() {
		for {
			env, err := stream.Recv()
			if err != nil {
				recvDone <- err
				return
			}
			out, _ := protojson.MarshalOptions{Multiline: false}.Marshal(env)
			fmt.Println(string(out))
		}
	}()

	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1024*1024), 1024*1024)
	sent := 0
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var r telemetryv1.Reading
		if err := protojson.Unmarshal(line, &r); err != nil {
			fmt.Fprintf(os.Stderr, "line %d: invalid v1 reading: %v\n", sent+1, err)
			os.Exit(1)
		}
		if err := stream.Send(&r); err != nil {
			fmt.Fprintf(os.Stderr, "send: %v\n", err)
			os.Exit(1)
		}
		sent++
	}
	if err := sc.Err(); err != nil {
		fmt.Fprintf(os.Stderr, "read input: %v\n", err)
		os.Exit(1)
	}
	if err := stream.CloseSend(); err != nil {
		fmt.Fprintf(os.Stderr, "close send: %v\n", err)
		os.Exit(1)
	}

	if err := <-recvDone; err != nil && err != io.EOF {
		fmt.Fprintf(os.Stderr, "receive: %v\n", err)
		os.Exit(1)
	}
	fmt.Fprintf(os.Stderr, "sent %d v1 readings\n", sent)
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
