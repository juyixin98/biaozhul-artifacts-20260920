package test_test

import (
	"context"
	"errors"
	"net"
	"os"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	gatewayv1 "github.com/example/compgw/gen/gateway/v1"
	telemetryv1 "github.com/example/compgw/gen/telemetry/v1"
	telemetryv2 "github.com/example/compgw/gen/telemetry/v2"
	cryptox "github.com/example/compgw/internal/crypto"
	"github.com/example/compgw/internal/mapping"
	"github.com/example/compgw/internal/server"
	"github.com/example/compgw/internal/store"
)

const testDSNDefault = "postgres://compgw:compgw@localhost:5432/compgw_test?sslmode=disable"

func dsn() string {
	if v := os.Getenv("COMPGW_TEST_PG_DSN"); v != "" {
		return v
	}
	return testDSNDefault
}

type harness struct {
	t        *testing.T
	st       *store.Store
	reg      *server.MappingRegistry
	client   gatewayv1.CompatGatewayClient
	shutdown func()
}

func newHarness(t *testing.T, maxInFlight int) *harness {
	t.Helper()
	ctx := context.Background()
	st, err := store.New(ctx, dsn())
	if err != nil {
		t.Skipf("postgresql not available (%v); skipping integration test", err)
	}
	if err := st.Migrate(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	// Fresh deterministic state for every test.
	if _, err := st.Pool().Exec(ctx, "TRUNCATE mappings, conversion_audit RESTART IDENTITY CASCADE"); err != nil {
		t.Fatalf("truncate: %v", err)
	}
	if err := st.Seed(ctx, mapping.SeedSpecs()); err != nil {
		t.Fatalf("seed: %v", err)
	}
	reg := server.NewMappingRegistry(st)
	reg.SetPollInterval(50 * time.Millisecond)
	if err := reg.Load(ctx); err != nil {
		t.Fatalf("load: %v", err)
	}
	watchCtx, cancelWatch := context.WithCancel(ctx)
	go reg.Watch(watchCtx)

	lis := bufconn.Listen(1024 * 1024)
	gs := grpc.NewServer()
	gatewayv1.RegisterCompatGatewayServer(gs, server.New(reg, st, maxInFlight))
	go gs.Serve(lis)

	dialer := func(context.Context, string) (net.Conn, error) { return lis.Dial() }
	conn, err := grpc.NewClient("passthrough://bufnet",
		grpc.WithContextDialer(dialer),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatalf("dial bufconn: %v", err)
	}
	return &harness{
		t: t, st: st, reg: reg,
		client: gatewayv1.NewCompatGatewayClient(conn),
		shutdown: func() {
			cancelWatch()
			conn.Close()
			gs.Stop()
			st.Close()
		},
	}
}

// openV2V1 opens a v2->v1 conversion stream.
func (h *harness) openV2V1(ctx context.Context) gatewayv1.CompatGateway_ConvertStreamV2ToV1Client {
	h.t.Helper()
	str, err := h.client.ConvertStreamV2ToV1(ctx)
	if err != nil {
		h.t.Fatalf("open stream: %v", err)
	}
	return str
}

func (h *harness) sign1(r *telemetryv1.TelemetryRecord) *telemetryv1.TelemetryRecord {
	r.Signature = cryptox.Sign(mapping.DefaultHMACKey, cryptox.CanonicalV1(r.GetDeviceId(),
		r.GetTimestampMs(), r.GetTempMilliC(), int32(r.GetStatus()), r.GetLabels(),
		r.GetRequestId(), r.BatteryMilliPct))
	return r
}

func (h *harness) sign2(r *telemetryv2.TelemetryRecord) *telemetryv2.TelemetryRecord {
	r.Signature = cryptox.Sign(mapping.DefaultHMACKey, cryptox.CanonicalV2(r.GetDeviceId(),
		r.GetObservedAtNs(), r.GetTemperatureUk(), int32(r.GetPhase()), r.GetLabels(),
		r.GetTraceId(), r.BatteryPctMicro))
	return r
}

func (h *harness) activate(name string) {
	h.t.Helper()
	resp, err := h.client.ActivateMapping(context.Background(),
		&gatewayv1.ActivateMappingRequest{Name: name})
	if err != nil {
		h.t.Fatalf("activate %s: %v", name, err)
	}
	h.t.Logf("activated %s version %d", resp.GetName(), resp.GetVersion())
}

func (h *harness) waitUnaryMaintenance(wantCarry bool) {
	h.t.Helper()
	rec := h.sign2(&telemetryv2.TelemetryRecord{
		DeviceId: "d", ObservedAtNs: 1_000_000, TemperatureUk: 294_400_000,
		Phase: telemetryv2.Phase_PHASE_MAINTENANCE, TraceId: "probe",
	})
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := h.client.ConvertV2ToV1(context.Background(),
			&gatewayv1.ConvertV2ToV1Request{Record: rec})
		if wantCarry && err == nil && resp.GetRecord().GetRawPhase() == 4 {
			return
		}
		if !wantCarry && err != nil {
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	h.t.Fatalf("timed out waiting for unary mapping switch (wantCarry=%v)", wantCarry)
}

// TestUnaryConversionsAndAudit covers renames/units/enum error+carry and
// verifies outcomes land in PostgreSQL audit storage.
func TestUnaryConversionsAndAudit(t *testing.T) {
	h := newHarness(t, 8)
	defer h.shutdown()

	bat := int64(870)
	v1 := h.sign1(&telemetryv1.TelemetryRecord{
		DeviceId: "sensor-A", TimestampMs: 1_727_000_000_123, TempMilliC: 21_250,
		Status: telemetryv1.Status_STATUS_ACTIVE, RequestId: "req-1",
		Labels: map[string]string{"site": "dc-1"}, BatteryMilliPct: &bat,
	})
	resp, err := h.client.ConvertV1ToV2(context.Background(),
		&gatewayv1.ConvertV1ToV2Request{Record: v1})
	if err != nil {
		t.Fatalf("v1->v2: %v", err)
	}
	out := resp.GetRecord()
	if out.GetObservedAtNs() != 1_727_000_000_123_000_000 ||
		out.GetTemperatureUk() != 294_400_000 ||
		out.GetPhase() != telemetryv2.Phase_PHASE_ACTIVE ||
		out.GetBatteryPctMicro() != 870_000 ||
		out.GetTraceId() != "req-1" {
		t.Fatalf("unexpected conversion: %v", out)
	}
	p := resp.GetProvenance()
	if p.GetSourceVersion() != "v1" || p.GetConvertedVersion() != "v2" ||
		p.GetMappingName() != "strict-v1-v2" || p.GetMappingVersion() == 0 {
		t.Fatalf("bad provenance: %v", p)
	}

	// strict v2->v1: maintenance is a locatable error carrying original 4.
	maint := h.sign2(&telemetryv2.TelemetryRecord{
		DeviceId: "d", ObservedAtNs: 1_000_000, TemperatureUk: 294_400_000,
		Phase: telemetryv2.Phase_PHASE_MAINTENANCE, TraceId: "tr-4",
	})
	_, err = h.client.ConvertV2ToV1(context.Background(),
		&gatewayv1.ConvertV2ToV1Request{Record: maint})
	if err == nil {
		t.Fatal("expected error for PHASE_MAINTENANCE under strict mapping")
	}
	st := status.Convert(err)
	if st.Code().String() != "InvalidArgument" {
		t.Fatalf("grpc code = %s, want InvalidArgument", st.Code())
	}
	var ce *gatewayv1.ConversionError
	for _, d := range st.Details() {
		if c, ok := d.(*gatewayv1.ConversionError); ok {
			ce = c
		}
	}
	if ce == nil || ce.GetCode() != gatewayv1.ConversionError_ENUM_UNMAPPABLE ||
		ce.GetField() != "phase" || ce.GetRecordId() != "tr-4" ||
		ce.GetOriginalEnum() != 4 {
		t.Fatalf("error not locatable / missing original enum: %v", ce)
	}

	// Audit rows persisted: one success and one enum failure.
	var okN, failN int
	var code, field string
	var carried *int32
	err = h.st.Pool().QueryRow(context.Background(),
		`SELECT count(*) FILTER (WHERE ok),
		        count(*) FILTER (WHERE NOT ok),
		        COALESCE(MAX(error_code) FILTER (WHERE NOT ok),''),
		        COALESCE(MAX(error_field) FILTER (WHERE NOT ok),''),
		        MAX(carried_enum)
		 FROM conversion_audit`).Scan(&okN, &failN, &code, &field, &carried)
	if err != nil {
		t.Fatalf("audit query: %v", err)
	}
	if okN < 1 || failN != 1 || code != "ENUM_UNMAPPABLE" || field != "phase" {
		t.Fatalf("audit rows unexpected: ok=%d fail=%d code=%q field=%q", okN, failN, code, field)
	}
	if carried != nil {
		t.Fatalf("strict failure must not persist carried enum, got %d", *carried)
	}

	// Hot switch to lenient: same record now converts with carried original.
	h.activate("lenient-v2-v1")
	h.waitUnaryMaintenance(true)
	resp2, err := h.client.ConvertV2ToV1(context.Background(),
		&gatewayv1.ConvertV2ToV1Request{Record: maint})
	if err != nil {
		t.Fatalf("lenient conversion: %v", err)
	}
	if resp2.GetRecord().GetStatus() != telemetryv1.Status_STATUS_UNSPECIFIED ||
		resp2.GetRecord().GetRawPhase() != 4 {
		t.Fatalf("carry failed: status=%v raw=%v",
			resp2.GetRecord().GetStatus(), resp2.GetRecord().GetRawPhase())
	}
	if resp2.GetProvenance().GetCarriedOriginalEnum() != 4 {
		t.Fatalf("provenance must carry original enum: %v", resp2.GetProvenance())
	}
}

// TestHotSwitchPinsStreams: unary picks up a new mapping at once; an
// already-open stream keeps using the mapping it opened with until it ends.
func TestHotSwitchPinsStreams(t *testing.T) {
	h := newHarness(t, 8)
	defer h.shutdown()

	open := h.openV2V1(context.Background())
	maint := h.sign2(&telemetryv2.TelemetryRecord{
		DeviceId: "d", ObservedAtNs: 1_000_000, TemperatureUk: 294_400_000,
		Phase: telemetryv2.Phase_PHASE_MAINTENANCE, TraceId: "before-switch",
	})
	if err := open.Send(&gatewayv1.StreamV2Request{Seq: 1,
		Item: &gatewayv1.ConvertV2ToV1Request{Record: maint}}); err != nil {
		t.Fatal(err)
	}
	got, err := open.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if got.GetError().GetCode() != gatewayv1.ConversionError_ENUM_UNMAPPABLE {
		t.Fatalf("pre-switch: expected enum error, got %v", got.GetOutcome())
	}

	// Hot switch while the stream stays open.
	h.activate("lenient-v2-v1")
	h.waitUnaryMaintenance(true)

	// Same open stream is pinned to strict: still an error.
	if err := open.Send(&gatewayv1.StreamV2Request{Seq: 2,
		Item: &gatewayv1.ConvertV2ToV1Request{Record: maint}}); err != nil {
		t.Fatal(err)
	}
	got, err = open.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if got.GetError().GetCode() != gatewayv1.ConversionError_ENUM_UNMAPPABLE {
		t.Fatalf("open stream must stay pinned to strict, got %v", got.GetOutcome())
	}

	// A new stream immediately uses lenient.
	if err := open.CloseSend(); err != nil {
		t.Fatal(err)
	}
	if _, err := open.Recv(); err == nil {
		t.Fatal("expected EOF after CloseSend")
	}
	fresh := h.openV2V1(context.Background())
	if err := fresh.Send(&gatewayv1.StreamV2Request{Seq: 1,
		Item: &gatewayv1.ConvertV2ToV1Request{Record: maint}}); err != nil {
		t.Fatal(err)
	}
	got, err = fresh.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if got.GetOk().GetRecord().GetRawPhase() != 4 {
		t.Fatalf("new stream should use lenient mapping, got %v", got.GetOutcome())
	}
	fresh.CloseSend()
}

// TestStreamPerItemErrorsAndOrdering: a bad record mid-stream produces a
// locatable per-item error outcome without killing the stream, in order.
func TestStreamPerItemErrorsAndOrdering(t *testing.T) {
	h := newHarness(t, 8)
	defer h.shutdown()

	str := h.openV2V1(context.Background())
	mk := func(seq uint64, trace string, phase telemetryv2.Phase, temp int64) *gatewayv1.StreamV2Request {
		rec := h.sign2(&telemetryv2.TelemetryRecord{
			DeviceId: "d", ObservedAtNs: 1_000_000, TemperatureUk: temp,
			Phase: phase, TraceId: trace,
		})
		return &gatewayv1.StreamV2Request{Seq: seq, Item: &gatewayv1.ConvertV2ToV1Request{Record: rec}}
	}
	reqs := []*gatewayv1.StreamV2Request{
		mk(1, "ok-1", telemetryv2.Phase_PHASE_ACTIVE, 294_400_000),
		mk(2, "bad-enum", telemetryv2.Phase_PHASE_MAINTENANCE, 294_400_000),
		mk(3, "ok-2", telemetryv2.Phase_PHASE_IDLE, 283_150_000),
	}
	for _, r := range reqs {
		if err := str.Send(r); err != nil {
			t.Fatal(err)
		}
	}
	str.CloseSend()

	var seqs []uint64
	var sawEnumError bool
	for {
		resp, err := str.Recv()
		if err != nil {
			break
		}
		seqs = append(seqs, resp.GetSeq())
		if resp.GetSeq() == 2 {
			ce := resp.GetError()
			if ce.GetCode() != gatewayv1.ConversionError_ENUM_UNMAPPABLE ||
				ce.GetRecordId() != "bad-enum" || ce.GetOriginalEnum() != 4 {
				t.Fatalf("mid-stream error not locatable: %v", ce)
			}
			sawEnumError = true
		}
		if resp.GetSeq() == 3 && resp.GetOk().GetRecord().GetTempMilliC() != 10_000 {
			t.Fatalf("item 3 conversion wrong: %v", resp.GetOk().GetRecord())
		}
	}
	if len(seqs) != 3 || seqs[0] != 1 || seqs[1] != 2 || seqs[2] != 3 {
		t.Fatalf("ordering/count wrong: %v", seqs)
	}
	if !sawEnumError {
		t.Fatal("enum error outcome missing")
	}
}

// TestStreamCancellationStopsConversion: canceling the client context stops
// the server-side stream promptly.
func TestStreamCancellationStopsConversion(t *testing.T) {
	h := newHarness(t, 8)
	defer h.shutdown()

	ctx, cancel := context.WithCancel(context.Background())
	str := h.openV2V1(ctx)
	rec := h.sign2(&telemetryv2.TelemetryRecord{
		DeviceId: "d", ObservedAtNs: 1_000_000, TemperatureUk: 294_400_000,
		Phase: telemetryv2.Phase_PHASE_ACTIVE, TraceId: "cancel-me",
	})
	for i := 0; i < 3; i++ {
		if err := str.Send(&gatewayv1.StreamV2Request{Seq: uint64(i + 1),
			Item: &gatewayv1.ConvertV2ToV1Request{Record: rec}}); err != nil {
			t.Fatal(err)
		}
	}
	for i := 0; i < 3; i++ {
		if _, err := str.Recv(); err != nil {
			t.Fatalf("unexpected early error: %v", err)
		}
	}
	cancel()
	done := make(chan error, 1)
	go func() { _, err := str.Recv(); done <- err }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("expected stream error after cancel")
		}
		if !errors.Is(err, context.Canceled) && status.Code(err).String() != "Canceled" {
			t.Fatalf("expected Canceled, got %v (%s)", err, status.Code(err))
		}
	case <-time.After(3 * time.Second):
		t.Fatal("stream did not stop promptly after cancellation")
	}
}

// TestBackpressureAllItemsArrive: with a 4-item server buffer, a burst of
// 200 messages is delivered fully and in order; cancellation then stops the
// producer from sending more.
func TestBackpressureAllItemsArrive(t *testing.T) {
	h := newHarness(t, 4)
	defer h.shutdown()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	str := h.openV2V1(ctx)

	const n = 200
	var sent int64
	sendErr := make(chan error, 1)
	go func() {
		for i := 1; i <= n; i++ {
			rec := h.sign2(&telemetryv2.TelemetryRecord{
				DeviceId: "bp", ObservedAtNs: int64(i) * 1_000_000,
				TemperatureUk: 294_400_000, Phase: telemetryv2.Phase_PHASE_ACTIVE,
				TraceId: "bp",
			})
			if err := str.Send(&gatewayv1.StreamV2Request{Seq: uint64(i),
				Item: &gatewayv1.ConvertV2ToV1Request{Record: rec}}); err != nil {
				sendErr <- err
				return
			}
			atomic.AddInt64(&sent, 1)
		}
		str.CloseSend()
	}()

	prev := uint64(0)
	count := 0
	for {
		resp, err := str.Recv()
		if err != nil {
			break
		}
		count++
		if resp.GetSeq() != prev+1 {
			t.Fatalf("ordering broken at %d after %d", resp.GetSeq(), prev)
		}
		prev = resp.GetSeq()
		if resp.GetOk() == nil {
			t.Fatalf("unexpected error outcome at %d", resp.GetSeq())
		}
	}
	if count != n {
		t.Fatalf("received %d of %d (bounded buffer dropped messages)", count, n)
	}
	if got := atomic.LoadInt64(&sent); got != n {
		t.Fatalf("producer sent %d of %d", got, n)
	}
}

// TestAbsentAndExplicitZeroOverWire verifies proto3 presence survives both
// the wire and conversion under the real gRPC stack.
func TestAbsentAndExplicitZeroOverWire(t *testing.T) {
	h := newHarness(t, 8)
	defer h.shutdown()

	zero := int64(0)
	withZero := h.sign1(&telemetryv1.TelemetryRecord{
		DeviceId: "d", TimestampMs: 1_000_000, TempMilliC: 0,
		Status: telemetryv1.Status_STATUS_IDLE, RequestId: "explicit", BatteryMilliPct: &zero,
	})
	absent := h.sign1(&telemetryv1.TelemetryRecord{
		DeviceId: "d", TimestampMs: 1_000_000, TempMilliC: 0,
		Status: telemetryv1.Status_STATUS_IDLE, RequestId: "absent",
	})
	r1, err := h.client.ConvertV1ToV2(context.Background(),
		&gatewayv1.ConvertV1ToV2Request{Record: withZero})
	if err != nil {
		t.Fatal(err)
	}
	r2, err := h.client.ConvertV1ToV2(context.Background(),
		&gatewayv1.ConvertV1ToV2Request{Record: absent})
	if err != nil {
		t.Fatal(err)
	}
	if r1.GetRecord().BatteryPctMicro == nil || r1.GetRecord().GetBatteryPctMicro() != 0 {
		t.Fatalf("explicit zero not preserved: %v", r1.GetRecord().BatteryPctMicro)
	}
	if r2.GetRecord().BatteryPctMicro != nil {
		t.Fatalf("absent field materialized over the wire: %v", r2.GetRecord().BatteryPctMicro)
	}
}
