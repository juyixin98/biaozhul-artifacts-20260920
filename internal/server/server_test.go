package server

import (
	"context"
	"io"
	"net"
	"sync"
	"testing"
	"time"

	gatewayv1 "github.com/p079/telegw/gen/gateway/v1"
	telemetryv1 "github.com/p079/telegw/gen/telemetry/v1"
	telemetryv2 "github.com/p079/telegw/gen/telemetry/v2"
	"github.com/p079/telegw/internal/convert"
	"github.com/p079/telegw/internal/registry"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func testRegistry(t *testing.T) *registry.Registry {
	t.Helper()
	reg, err := registry.NewInMemory("strict",
		convert.Mapping{Version: "strict", Enum: convert.EnumError, Rounding: convert.RoundStrict},
		convert.Mapping{Version: "carry", Enum: convert.EnumCarry, Rounding: convert.RoundHalfEven},
	)
	if err != nil {
		t.Fatal(err)
	}
	return reg
}

// startBufconn spins up an in-process gRPC server with the given buffer size.
func startBufconn(t *testing.T, bufSize int) (gatewayv1.GatewayClient, *Server, func()) {
	t.Helper()
	lis := bufconn.Listen(1024 * 1024)
	srv := New(testRegistry(t), nil, bufSize, nil)
	grpcSrv := grpc.NewServer()
	gatewayv1.RegisterGatewayServer(grpcSrv, srv)
	go grpcSrv.Serve(lis)
	conn, err := grpc.NewClient("passthrough:///bufconn",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	cleanup := func() { conn.Close(); grpcSrv.Stop(); lis.Close() }
	return gatewayv1.NewGatewayClient(conn), srv, cleanup
}

func i32(v int32) *int32 { return &v }
func i64(v int64) *int64 { return &v }

func TestStreamV1ToV2EndToEnd(t *testing.T) {
	client, srv, cleanup := startBufconn(t, 8)
	defer cleanup()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	stream, err := client.ConvertV1ToV2(ctx)
	if err != nil {
		t.Fatal(err)
	}
	in := &telemetryv1.Reading{
		DeviceId: "dev-1", TempCelsiusE2: i32(2150),
		Condition: telemetryv1.Condition_OK, ObservedAtUnixMs: 1_758_614_400_000, SeqNo: 1,
	}
	if err := stream.Send(in); err != nil {
		t.Fatal(err)
	}
	if err := stream.CloseSend(); err != nil {
		t.Fatal(err)
	}
	env, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	v2 := env.GetV2()
	if v2 == nil {
		t.Fatalf("want v2 payload, got %v", env.Result)
	}
	if v2.DeviceUid != "dev-1" || v2.GetTemperatureMillikelvin() != 2_753_000 {
		t.Errorf("bad conversion: %+v", v2)
	}
	// Provenance: every output carries source and converter versions.
	m := env.Meta
	if m.SourceVersion != "telemetry/v1" || m.TargetVersion != "telemetry/v2" {
		t.Errorf("bad provenance: %+v", m)
	}
	if m.ConverterVersion != convert.ConverterVersion || m.MappingVersion != "strict" {
		t.Errorf("bad versions: %+v", m)
	}
	if m.RecordId == "" {
		t.Error("record id missing")
	}
	if _, err := stream.Recv(); err != io.EOF {
		t.Errorf("want EOF, got %v", err)
	}
	if srv.convertedOK.Load() != 1 {
		t.Errorf("convertedOK = %d", srv.convertedOK.Load())
	}
}

// Version hot-swap: the same v2 enum value fails under "strict" and is
// carried under "carry" after ReloadMapping, mid-stream.
func TestMappingHotSwapMidStream(t *testing.T) {
	client, _, cleanup := startBufconn(t, 8)
	defer cleanup()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	stream, err := client.ConvertV2ToV1(ctx)
	if err != nil {
		t.Fatal(err)
	}
	mkReading := func(seq uint64) *telemetryv2.Reading {
		return &telemetryv2.Reading{
			DeviceUid: "dev-9", Condition: telemetryv2.Condition_MAINTENANCE,
			ObservedAt: timestamppb.New(time.Unix(1_758_614_400, 0)), SeqNo: seq,
		}
	}

	// 1. strict mapping: locatable error, not a silent zero.
	if err := stream.Send(mkReading(1)); err != nil {
		t.Fatal(err)
	}
	env, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	cerr := env.GetError()
	if cerr == nil {
		t.Fatalf("strict mapping: want error envelope, got %v", env.Result)
	}
	if cerr.FieldPath != "condition" || cerr.RawValue == "" {
		t.Errorf("error not locatable: %+v", cerr)
	}

	// 2. Hot-swap to carry mapping.
	resp, err := client.ReloadMapping(ctx, &gatewayv1.ReloadMappingRequest{MappingVersion: "carry"})
	if err != nil {
		t.Fatal(err)
	}
	if resp.ActiveMappingVersion != "carry" {
		t.Fatalf("active = %q", resp.ActiveMappingVersion)
	}

	// 3. Same value now converts, carrying the raw enum.
	if err := stream.Send(mkReading(2)); err != nil {
		t.Fatal(err)
	}
	env, err = stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	v1 := env.GetV1()
	if v1 == nil {
		t.Fatalf("carry mapping: want v1 payload, got %v", env.Result)
	}
	if v1.Condition != telemetryv1.Condition_CONDITION_UNSPECIFIED {
		t.Errorf("condition = %v", v1.Condition)
	}
	if env.Meta.Attributes["unmapped_condition_raw"] != "4" {
		t.Errorf("raw enum not carried: %v", env.Meta.Attributes)
	}
	if env.Meta.MappingVersion != "carry" {
		t.Errorf("meta mapping = %q", env.Meta.MappingVersion)
	}
	if err := stream.CloseSend(); err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Recv(); err != io.EOF {
		t.Errorf("want EOF, got %v", err)
	}
}

// Half-stream failure: the client cancels after a few records; the server
// must stop converting promptly and must not process the unsent backlog.
func TestClientCancelStopsConversion(t *testing.T) {
	client, srv, cleanup := startBufconn(t, 4)
	defer cleanup()

	ctx, cancel := context.WithCancel(context.Background())
	stream, err := client.ConvertV1ToV2(ctx)
	if err != nil {
		t.Fatal(err)
	}
	send := func(seq uint64) {
		if err := stream.Send(&telemetryv1.Reading{
			DeviceId: "d", ObservedAtUnixMs: 1, SeqNo: seq,
		}); err != nil {
			t.Logf("send %d: %v (acceptable after cancel)", seq, err)
		}
	}
	send(1)
	if _, err := stream.Recv(); err != nil {
		t.Fatal(err)
	}
	cancel() // client dies mid-stream

	// Server must observe cancellation: further Recv fails.
	deadline := time.After(3 * time.Second)
	for {
		_, err := stream.Recv()
		if err != nil {
			break // canceled, as required
		}
		select {
		case <-deadline:
			t.Fatal("server did not stop after client cancel")
		default:
		}
	}
	// Drain server-side state: no runaway conversion.
	time.Sleep(100 * time.Millisecond)
	if got := srv.inFlight.Load(); got != 0 {
		t.Errorf("in_flight = %d after cancel, want 0", got)
	}
	if got := srv.convertedOK.Load(); got > 1 {
		t.Errorf("converted %d records after only 1 was consumed", got)
	}
}

// fakeStream is a deterministic recvSendStream for backpressure testing:
// Send blocks until the test releases it, so the bounded buffer is the
// only place records can accumulate.
type fakeStream struct {
	ctx       context.Context
	reqs      []*telemetryv1.Reading
	mu        sync.Mutex
	recvCalls int
	sent      []*gatewayv1.Envelope
	sendGate  chan struct{}
}

func (f *fakeStream) Recv() (*telemetryv1.Reading, error) {
	f.mu.Lock()
	i := f.recvCalls
	f.recvCalls++
	f.mu.Unlock()
	if i < len(f.reqs) {
		return f.reqs[i], nil
	}
	<-f.ctx.Done() // like a real stream with no more client data
	return nil, f.ctx.Err()
}

func (f *fakeStream) Send(env *gatewayv1.Envelope) error {
	<-f.sendGate // downstream backpressure: blocks until released
	f.mu.Lock()
	f.sent = append(f.sent, env)
	f.mu.Unlock()
	return nil
}

func (f *fakeStream) Context() context.Context { return f.ctx }

func (f *fakeStream) recvs() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.recvCalls
}

// Backpressure: with buffer size 1 and a blocked downstream, the server
// must not read more than buffer+1 records ahead.
func TestBackpressureBoundsBuffer(t *testing.T) {
	srv := New(testRegistry(t), nil, 1, nil)
	ctx, cancel := context.WithCancel(context.Background())
	fs := &fakeStream{
		ctx:      ctx,
		sendGate: make(chan struct{}), // never released until the end
		reqs: []*telemetryv1.Reading{
			{DeviceId: "a", ObservedAtUnixMs: 1},
			{DeviceId: "b", ObservedAtUnixMs: 1},
			{DeviceId: "c", ObservedAtUnixMs: 1},
			{DeviceId: "d", ObservedAtUnixMs: 1},
			{DeviceId: "e", ObservedAtUnixMs: 1},
		},
	}
	done := make(chan error, 1)
	go func() {
		done <- serve[*telemetryv1.Reading](fs, srv, "v1->v2", convert.SourceV1, convert.SourceV2,
			func(m convert.Mapping, req *telemetryv1.Reading) (any, *convert.Error, convert.Attrs, int) {
				out, attrs, unknown, err := convert.V1ToV2(m, req)
				if err != nil {
					ce, _ := convert.AsError(err)
					return nil, ce, attrs, unknown
				}
				return out, nil, attrs, unknown
			})
	}()

	// Let the pipeline settle: 1 record stuck in Send, 1 in the buffer,
	// 1 blocked in the recv select. No more may be read.
	time.Sleep(200 * time.Millisecond)
	if got := fs.recvs(); got > 3 {
		t.Fatalf("server read %d records with buffer=1 and blocked downstream; want <= 3", got)
	}

	// Cancellation must unblock everything.
	cancel()
	close(fs.sendGate)
	select {
	case err := <-done:
		if err == nil {
			t.Log("serve returned nil after cancel (send path won the race)")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("serve did not return after cancel")
	}
	if got := srv.inFlight.Load(); got != 0 {
		t.Errorf("in_flight = %d after shutdown", got)
	}
}

func TestReloadMappingUnknownVersion(t *testing.T) {
	client, _, cleanup := startBufconn(t, 4)
	defer cleanup()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if _, err := client.ReloadMapping(ctx, &gatewayv1.ReloadMappingRequest{MappingVersion: "nope"}); err == nil {
		t.Fatal("expected error for unknown mapping version")
	}
}
