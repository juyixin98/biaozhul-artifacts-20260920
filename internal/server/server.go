// Package server implements the streaming gRPC gateway: bounded in-flight
// buffering for backpressure, context-cancellation-aware conversion, and
// a per-record audit trail.
package server

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"sync/atomic"

	gatewayv1 "github.com/p079/telegw/gen/gateway/v1"
	telemetryv1 "github.com/p079/telegw/gen/telemetry/v1"
	telemetryv2 "github.com/p079/telegw/gen/telemetry/v2"
	"github.com/p079/telegw/internal/convert"
	"github.com/p079/telegw/internal/registry"
	"github.com/p079/telegw/internal/store"
	"google.golang.org/protobuf/proto"
)

// DefaultBufferSize bounds how many received-but-unprocessed records the
// server may hold per stream. Beyond this the receive loop blocks and
// gRPC flow control pushes back on the client.
const DefaultBufferSize = 32

// Server implements gatewayv1.GatewayServer.
type Server struct {
	gatewayv1.UnimplementedGatewayServer

	reg     *registry.Registry
	store   *store.Store // may be nil (no-op)
	bufSize int
	log     *slog.Logger

	convertedOK   atomic.Int64
	convertErrors atomic.Int64
	inFlight      atomic.Int64
	unknownFields atomic.Int64
	auditFailures atomic.Int64
}

// New builds a Server. st may be nil to disable persistence.
func New(reg *registry.Registry, st *store.Store, bufSize int, log *slog.Logger) *Server {
	if bufSize <= 0 {
		bufSize = DefaultBufferSize
	}
	if log == nil {
		log = slog.Default()
	}
	return &Server{reg: reg, store: st, bufSize: bufSize, log: log}
}

// recvSendStream abstracts the two generated server stream types.
type recvSendStream[Req proto.Message] interface {
	Recv() (Req, error)
	Send(*gatewayv1.Envelope) error
	Context() context.Context
}

// convertFn converts one request into a response Envelope (payload or
// error), filling Meta except Seq.
type convertFn[Req proto.Message] func(m convert.Mapping, req Req) (payload any, convErr *convert.Error, attrs convert.Attrs, unknown int)

// ConvertV1ToV2 streams v1 readings in, v2 envelopes out.
func (s *Server) ConvertV1ToV2(stream gatewayv1.Gateway_ConvertV1ToV2Server) error {
	return serve(stream, s, "v1->v2", convert.SourceV1, convert.SourceV2,
		func(m convert.Mapping, req *telemetryv1.Reading) (any, *convert.Error, convert.Attrs, int) {
			out, attrs, unknown, err := convert.V1ToV2(m, req)
			if err != nil {
				ce, _ := convert.AsError(err)
				return nil, ce, attrs, unknown
			}
			return out, nil, attrs, unknown
		})
}

// ConvertV2ToV1 streams v2 readings in, v1 envelopes out.
func (s *Server) ConvertV2ToV1(stream gatewayv1.Gateway_ConvertV2ToV1Server) error {
	return serve(stream, s, "v2->v1", convert.SourceV2, convert.SourceV1,
		func(m convert.Mapping, req *telemetryv2.Reading) (any, *convert.Error, convert.Attrs, int) {
			out, attrs, unknown, err := convert.V2ToV1(m, req)
			if err != nil {
				ce, _ := convert.AsError(err)
				return nil, ce, attrs, unknown
			}
			return out, nil, attrs, unknown
		})
}

// serve is the shared streaming pipeline (a free function because Go
// methods cannot have their own type parameters):
//
//	recv goroutine -> bounded channel -> convert+audit -> Send
//
// The bounded channel is the backpressure mechanism: if the downstream
// client stops reading, Send blocks, the channel fills, and the receive
// loop stops pulling from the transport. Cancellation of the stream
// context stops both halves promptly.
func serve[Req proto.Message](
	stream recvSendStream[Req],
	s *Server,
	direction, srcVer, dstVer string,
	conv convertFn[Req],
) error {
	ctx := stream.Context()
	type item struct {
		req Req
		seq int64
	}
	buf := make(chan item, s.bufSize)
	recvDone := make(chan error, 1)

	go func() {
		defer close(buf)
		var seq int64
		for {
			req, err := stream.Recv()
			if err != nil {
				recvDone <- err
				return
			}
			select {
			case buf <- item{req: req, seq: seq}:
				seq++
			case <-ctx.Done():
				recvDone <- ctx.Err()
				return
			}
		}
	}()

	for it := range buf {
		s.inFlight.Add(1)
		env := processOne(ctx, s, it.req, it.seq, direction, srcVer, dstVer, conv)
		s.inFlight.Add(-1)
		if err := stream.Send(env); err != nil {
			return err // client gone or transport error: stop converting
		}
	}

	err := <-recvDone
	if errors.Is(err, io.EOF) {
		return nil
	}
	return err // includes context.Canceled / DeadlineExceeded
}

// processOne converts one record, audits it, and builds the envelope.
func processOne[Req proto.Message](
	ctx context.Context,
	s *Server,
	req Req,
	seq int64,
	direction, srcVer, dstVer string,
	conv convertFn[Req],
) *gatewayv1.Envelope {
	m := s.reg.Active() // snapshot per record: hot-swap takes effect on the next record
	payload, convErr, attrs, unknown := conv(m, req)
	if unknown > 0 {
		s.unknownFields.Add(int64(unknown))
	}
	if attrs == nil {
		attrs = convert.Attrs{}
	}

	raw, _ := proto.Marshal(req)
	sum := sha256.Sum256(raw)
	recordID := hex.EncodeToString(sum[:])[:16]

	meta := &gatewayv1.Meta{
		RecordId:         recordID,
		Seq:              seq,
		SourceVersion:    srcVer,
		TargetVersion:    dstVer,
		ConverterVersion: convert.ConverterVersion,
		MappingVersion:   m.Version,
		Attributes:       attrs,
	}

	env := &gatewayv1.Envelope{Meta: meta}
	audit := store.AuditRecord{
		RecordID:         recordID,
		Direction:        direction,
		SourceVersion:    srcVer,
		TargetVersion:    dstVer,
		ConverterVersion: convert.ConverterVersion,
		MappingVersion:   m.Version,
		PayloadSHA256:    hex.EncodeToString(sum[:]),
	}

	if convErr != nil {
		env.Result = &gatewayv1.Envelope_Error{Error: &gatewayv1.ConversionError{
			FieldPath: convErr.FieldPath,
			Reason:    convErr.Reason,
			RawValue:  convErr.RawValue,
		}}
		audit.Status = "error"
		audit.ErrorDetail = &store.ErrorDetail{
			FieldPath: convErr.FieldPath,
			Reason:    convErr.Reason,
			RawValue:  convErr.RawValue,
		}
		s.convertErrors.Add(1)
	} else {
		switch p := payload.(type) {
		case *telemetryv1.Reading:
			env.Result = &gatewayv1.Envelope_V1{V1: p}
		case *telemetryv2.Reading:
			env.Result = &gatewayv1.Envelope_V2{V2: p}
		default:
			env.Result = &gatewayv1.Envelope_Error{Error: &gatewayv1.ConversionError{
				FieldPath: "<internal>",
				Reason:    fmt.Sprintf("unexpected payload type %T", payload),
			}}
			audit.Status = "error"
			s.convertErrors.Add(1)
		}
		if audit.Status == "" {
			audit.Status = "ok"
			s.convertedOK.Add(1)
		}
	}

	if s.store != nil {
		if err := s.store.InsertAudit(ctx, audit); err != nil {
			s.auditFailures.Add(1)
			s.log.Warn("audit insert failed", "record_id", recordID, "error", err)
			meta.Attributes["audit_status"] = "failed: " + err.Error()
		} else {
			meta.Attributes["audit_status"] = "persisted"
		}
	}
	return env
}

// ReloadMapping hot-swaps the active mapping version.
func (s *Server) ReloadMapping(ctx context.Context, req *gatewayv1.ReloadMappingRequest) (*gatewayv1.ReloadMappingResponse, error) {
	if err := s.reg.Activate(req.MappingVersion); err != nil {
		return nil, err
	}
	return &gatewayv1.ReloadMappingResponse{
		ActiveMappingVersion: s.reg.Active().Version,
		AvailableVersions:    s.reg.Versions(),
	}, nil
}

// GetStats reports cumulative counters.
func (s *Server) GetStats(ctx context.Context, _ *gatewayv1.GetStatsRequest) (*gatewayv1.GetStatsResponse, error) {
	return &gatewayv1.GetStatsResponse{
		ConvertedOk:          s.convertedOK.Load(),
		ConversionErrors:     s.convertErrors.Load(),
		InFlight:             s.inFlight.Load(),
		UnknownFieldsDropped: s.unknownFields.Load(),
		AuditFailures:        s.auditFailures.Load(),
	}, nil
}
