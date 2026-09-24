// Command demo is a dual-version client for the compatibility gateway.
//
// Subcommands:
//
//	demo v1-to-v2        signed unary v1 record -> v2
//	demo v2-to-v1        signed unary v2 record -> v1 (try -phase 4)
//	demo stream-v2-v1    stream N records, one in maintenance + one fractional
//	demo activate        hot-switch active v2->v1 mapping (strict|lenient)
//	demo mapping         show active v1->v2 mapping
//	demo gen-examples    emit the sample JSON inputs under examples/
//	demo backpressure    stream slower than server, showing bounded buffering
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math/rand"
	"os"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"

	gatewayv1 "github.com/example/compgw/gen/gateway/v1"
	telemetryv1 "github.com/example/compgw/gen/telemetry/v1"
	telemetryv2 "github.com/example/compgw/gen/telemetry/v2"
	cryptox "github.com/example/compgw/internal/crypto"
	"github.com/example/compgw/internal/mapping"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	addr := envOr("COMPGW_GRPC_ADDR", "127.0.0.1:50051")
	key := envOr("COMPGW_HMAC_KEY", mapping.DefaultHMACKey)
	conn, err := grpc.NewClient(addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	c := gatewayv1.NewCompatGatewayClient(conn)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	switch os.Args[1] {
	case "v1-to-v2":
		cmdV1ToV2(ctx, c, key, os.Args[2:])
	case "v2-to-v1":
		cmdV2ToV1(ctx, c, key, os.Args[2:])
	case "stream-v2-v1":
		cmdStream(ctx, c, key, os.Args[2:])
	case "activate":
		cmdActivate(ctx, c, os.Args[2:])
	case "mapping":
		cmdMapping(ctx, c)
	case "gen-examples":
		cmdGenExamples(key)
	case "backpressure":
		cmdBackpressure(ctx, c, key, os.Args[2:])
	default:
		usage()
	}
}

func cmdV1ToV2(ctx context.Context, c gatewayv1.CompatGatewayClient, key string, args []string) {
	fs := flag.NewFlagSet("v1-to-v2", flag.ExitOnError)
	device := fs.String("device", "sensor-A", "")
	ts := fs.Int64("ts-ms", 1_727_000_000_123, "timestamp in ms")
	temp := fs.Int64("temp-mc", 21_250, "temperature milli-C")
	statusv := fs.Int("status", 1, "v1 status enum number")
	reqID := fs.String("req", "req-0001", "")
	bat := fs.Int64("battery-mp", 870, "battery milli-percent")
	noBat := fs.Bool("no-battery", false, "leave battery unset (vs explicit zero)")
	badSig := fs.Bool("bad-signature", false, "send a wrong HMAC")
	fs.Parse(args)

	rec := signV1(&telemetryv1.TelemetryRecord{
		DeviceId: *device, TimestampMs: *ts, TempMilliC: *temp,
		Status: telemetryv1.Status(*statusv), RequestId: *reqID,
		Labels: map[string]string{"site": "dc-1"},
	}, key)
	if !*noBat {
		rec.BatteryMilliPct = bat
		rec = signV1(rec, key)
	}
	if *badSig {
		rec.Signature[0] ^= 0xFF
	}
	callUnary(ctx, c, rec)
}

func cmdV2ToV1(ctx context.Context, c gatewayv1.CompatGatewayClient, key string, args []string) {
	fs := flag.NewFlagSet("v2-to-v1", flag.ExitOnError)
	device := fs.String("device", "sensor-A", "")
	ts := fs.Int64("ts-ns", 1_727_000_000_123_000_000, "timestamp in ns")
	temp := fs.Int64("temp-uk", 294_400_000, "temperature micro-K")
	phase := fs.Int("phase", 1, "v2 phase enum number (4=maintenance, unmappable to v1)")
	trace := fs.String("trace", "trace-0001", "")
	bat := fs.Int64("battery-upm", 870_000, "battery micro-percent")
	fs.Parse(args)

	rec := signV2(&telemetryv2.TelemetryRecord{
		DeviceId: *device, ObservedAtNs: *ts, TemperatureUk: *temp,
		Phase: telemetryv2.Phase(*phase), TraceId: *trace,
		Labels: map[string]string{"site": "dc-1"}, BatteryPctMicro: bat,
	}, key)
	resp, err := c.ConvertV2ToV1(ctx, &gatewayv1.ConvertV2ToV1Request{Record: rec})
	if err != nil {
		printStatusErr(err)
		return
	}
	printJSON("response", resp)
}

func cmdStream(ctx context.Context, c gatewayv1.CompatGatewayClient, key string, args []string) {
	fs := flag.NewFlagSet("stream-v2-v1", flag.ExitOnError)
	n := fs.Int("n", 5, "")
	fs.Parse(args)

	stream, err := c.ConvertStreamV2ToV1(ctx)
	if err != nil {
		log.Fatal(err)
	}
	rng := rand.New(rand.NewSource(1))
	for i := 0; i < *n; i++ {
		phase := int64(1 + i%3)
		if i == 2 {
			phase = 4 // maintenance: locatable error or carried, depending on active mapping
		}
		temp := int64(294_400_000 + rng.Int63n(5_000)) // often not a multiple of 1000
		rec := signV2(&telemetryv2.TelemetryRecord{
			DeviceId:      fmt.Sprintf("sensor-%d", i%2),
			ObservedAtNs:  1_727_000_000_000_000_000 + int64(i)*1_000_000,
			TemperatureUk: temp, Phase: telemetryv2.Phase(phase),
			TraceId: fmt.Sprintf("trace-%04d", i),
			Labels:  map[string]string{"i": fmt.Sprint(i)},
		}, key)
		if err := stream.Send(&gatewayv1.StreamV2Request{Seq: uint64(i + 1),
			Item: &gatewayv1.ConvertV2ToV1Request{Record: rec}}); err != nil {
			log.Fatalf("send %d: %v", i, err)
		}
	}
	if err := stream.CloseSend(); err != nil {
		log.Fatalf("close send: %v", err)
	}
	for i := 0; i < *n; i++ {
		resp, err := stream.Recv()
		if err != nil {
			log.Fatalf("recv: %v", err)
		}
		printJSON(fmt.Sprintf("stream item %d", resp.GetSeq()), resp.GetOutcome())
	}
}

func cmdActivate(ctx context.Context, c gatewayv1.CompatGatewayClient, args []string) {
	fs := flag.NewFlagSet("activate", flag.ExitOnError)
	name := fs.String("name", "", "existing mapping name, e.g. strict-v2-v1 / lenient-v2-v1")
	specFile := fs.String("spec-file", "", "path to a mapping spec JSON to upsert+activate")
	fs.Parse(args)
	req := &gatewayv1.ActivateMappingRequest{Name: *name}
	if *specFile != "" {
		b, err := os.ReadFile(*specFile)
		if err != nil {
			log.Fatal(err)
		}
		req.Name = ""
		req.SpecJson = string(b)
	}
	resp, err := c.ActivateMapping(ctx, req)
	if err != nil {
		log.Fatalf("activate: %v", err)
	}
	printJSON("activated", resp)
}

func cmdMapping(ctx context.Context, c gatewayv1.CompatGatewayClient) {
	resp, err := c.GetMapping(ctx, &gatewayv1.GetMappingRequest{})
	if err != nil {
		log.Fatal(err)
	}
	printJSON("active v1->v2 mapping", resp)
}

func cmdBackpressure(ctx context.Context, c gatewayv1.CompatGatewayClient, key string, args []string) {
	fs := flag.NewFlagSet("backpressure", flag.ExitOnError)
	n := fs.Int("n", 500, "")
	slow := fs.Duration("recv-pause", 20*time.Millisecond, "client delay between receives")
	fs.Parse(args)

	stream, err := c.ConvertStreamV2ToV1(context.Background())
	if err != nil {
		log.Fatal(err)
	}
	go func() {
		for i := 0; i < *n; i++ {
			rec := signV2(&telemetryv2.TelemetryRecord{
				DeviceId: "bp", ObservedAtNs: int64(i+1) * 1_000_000,
				TemperatureUk: 294_400_000, Phase: telemetryv2.Phase_PHASE_ACTIVE,
				TraceId: fmt.Sprintf("bp-%04d", i),
			}, key)
			if err := stream.Send(&gatewayv1.StreamV2Request{Seq: uint64(i + 1),
				Item: &gatewayv1.ConvertV2ToV1Request{Record: rec}}); err != nil {
				log.Printf("send stopped at %d: %v (backpressure/cancel works)", i, err)
				return
			}
			if i%50 == 0 {
				log.Printf("sent %d", i)
			}
		}
		stream.CloseSend()
	}()
	for {
		resp, err := stream.Recv()
		if err != nil {
			log.Printf("stream ended: %v", err)
			return
		}
		time.Sleep(*slow)
		if resp.GetSeq()%50 == 0 {
			log.Printf("received up to %d", resp.GetSeq())
		}
	}
}

// ---- sample inputs ----

func cmdGenExamples(key string) {
	v1Happy := signV1(&telemetryv1.TelemetryRecord{
		DeviceId: "sensor-A", TimestampMs: 1_727_000_000_123, TempMilliC: 21_250,
		Status: telemetryv1.Status_STATUS_ACTIVE, RequestId: "req-0001",
		Labels:          map[string]string{"site": "dc-1", "rack": "r-7"},
		BatteryMilliPct: ptr(int64(870)),
	}, key)

	v1ExplicitZero := signV1(&telemetryv1.TelemetryRecord{
		DeviceId: "sensor-A", TimestampMs: 1_727_000_000_123, TempMilliC: 21_250,
		Status: telemetryv1.Status_STATUS_IDLE, RequestId: "req-0002",
		BatteryMilliPct: ptr(int64(0)),
	}, key)

	v1Unset := &telemetryv1.TelemetryRecord{
		DeviceId: "sensor-A", TimestampMs: 1_727_000_000_123, TempMilliC: 21_250,
		Status: telemetryv1.Status_STATUS_IDLE, RequestId: "req-0003",
	}
	v1Unset = signV1(v1Unset, key)

	v2Maintenance := signV2(&telemetryv2.TelemetryRecord{
		DeviceId: "sensor-B", ObservedAtNs: 1_727_000_000_123_000_000,
		TemperatureUk: 294_400_000, Phase: telemetryv2.Phase_PHASE_MAINTENANCE,
		TraceId: "trace-9001", BatteryPctMicro: ptr2(int64(870_000)),
	}, key)

	v2Fractional := signV2(&telemetryv2.TelemetryRecord{
		DeviceId: "sensor-B", ObservedAtNs: 1_727_000_000_123_456_789, // sub-ms
		TemperatureUk: 294_400_499, // not /1000
		Phase:         telemetryv2.Phase_PHASE_ACTIVE, TraceId: "trace-9002",
	}, key)

	// v1 -> v2 multiplies by 1,000,000 (ms->ns): a near-max int64 ms value
	// overflows int64 ns. This is a real overflow path (division v2->v1 cannot
	// overflow int64, multiplication/offset v1->v2 can).
	v1Overflow := signV1(&telemetryv1.TelemetryRecord{
		DeviceId: "sensor-A", TimestampMs: 9_223_372_036_854_775_000, TempMilliC: 21_250,
		Status: telemetryv1.Status_STATUS_ACTIVE, RequestId: "req-9003",
	}, key)

	writeExample("examples/v1_happy.json", jsonInput{SkipSignatureVerify: false, RecordJSON: protoJSON(v1Happy)})
	writeExample("examples/v1_battery_explicit_zero.json", jsonInput{RecordJSON: protoJSON(v1ExplicitZero)})
	writeExample("examples/v1_battery_unset.json", jsonInput{RecordJSON: protoJSON(v1Unset)})
	writeExample("examples/v2_maintenance_enum.json", jsonInput{RecordJSON: protoJSON(v2Maintenance)})
	writeExample("examples/v2_fractional_rounding.json", jsonInput{RecordJSON: protoJSON(v2Fractional)})
	writeExample("examples/v1_integer_overflow.json", jsonInput{RecordJSON: protoJSON(v1Overflow)})
	fmt.Println("wrote examples/*.json")
}

type jsonInput struct {
	SkipSignatureVerify bool            `json:"skip_signature_verify"`
	RecordJSON          json.RawMessage `json:"record_json"`
}

// ---- helpers ----

func callUnary(ctx context.Context, c gatewayv1.CompatGatewayClient, rec *telemetryv1.TelemetryRecord) {
	resp, err := c.ConvertV1ToV2(ctx, &gatewayv1.ConvertV1ToV2Request{Record: rec})
	if err != nil {
		printStatusErr(err)
		return
	}
	printJSON("response", resp)
}

func signV1(rec *telemetryv1.TelemetryRecord, key string) *telemetryv1.TelemetryRecord {
	rec.Signature = cryptox.Sign(key, cryptox.CanonicalV1(
		rec.GetDeviceId(), rec.GetTimestampMs(), rec.GetTempMilliC(), int32(rec.GetStatus()),
		rec.GetLabels(), rec.GetRequestId(), rec.BatteryMilliPct))
	return rec
}

func signV2(rec *telemetryv2.TelemetryRecord, key string) *telemetryv2.TelemetryRecord {
	rec.Signature = cryptox.Sign(key, cryptox.CanonicalV2(
		rec.GetDeviceId(), rec.GetObservedAtNs(), rec.GetTemperatureUk(), int32(rec.GetPhase()),
		rec.GetLabels(), rec.GetTraceId(), rec.BatteryPctMicro))
	return rec
}

func ptr(v int64) *int64  { return &v }
func ptr2(v int64) *int64 { return &v }

func printJSON(label string, v any) {
	fmt.Printf("=== %s ===\n", label)
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func protoJSON(m any) json.RawMessage {
	// Compact, stable representation using the proto-JSON marshaller would need
	// protojson; the structs here are simple enough for encoding/json via the
	// generated getters. Use a typed envelope built from exported fields.
	b, _ := json.Marshal(m)
	return b
}

func writeExample(path string, in jsonInput) {
	b, _ := json.MarshalIndent(in, "", "  ")
	if err := os.WriteFile(path, b, 0o644); err != nil {
		log.Fatal(err)
	}
}

func printStatusErr(err error) {
	st := status.Convert(err)
	fmt.Printf("RPC FAILED: code=%s message=%q\n", st.Code(), st.Message())
	for _, d := range st.Details() {
		if ce, ok := d.(*gatewayv1.ConversionError); ok {
			b, _ := json.MarshalIndent(ce, "", "  ")
			fmt.Printf("locatable error detail:\n%s\n", b)
		}
	}
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func usage() {
	fmt.Fprint(os.Stderr, `usage: demo <v1-to-v2|v2-to-v1|stream-v2-v1|activate|mapping|gen-examples|backpressure> [flags]
env: COMPGW_GRPC_ADDR, COMPGW_HMAC_KEY
`)
	os.Exit(2)
}
