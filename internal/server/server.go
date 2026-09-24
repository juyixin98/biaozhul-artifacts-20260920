package server

import (
	"context"
	"errors"
	"io"

	gatewayv1 "github.com/example/compgw/gen/gateway/v1"
	"github.com/example/compgw/internal/mapping"
	"github.com/example/compgw/internal/store"
	"github.com/example/compgw/internal/transform"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Server implements gateway.v1.CompatGateway.
type Server struct {
	gatewayv1.UnimplementedCompatGatewayServer

	registry *MappingRegistry
	store    *store.Store
	// MaxInFlight bounds the number of buffered, not-yet-answered stream items.
	// The receiver blocks once it is full: real stream backpressure.
	maxInFlight int
}

func New(reg *MappingRegistry, st *store.Store, maxInFlight int) *Server {
	if maxInFlight <= 0 {
		maxInFlight = 64
	}
	return &Server{registry: reg, store: st, maxInFlight: maxInFlight}
}

func (s *Server) ConvertV1ToV2(ctx context.Context, req *gatewayv1.ConvertV1ToV2Request) (*gatewayv1.ConvertV1ToV2Response, error) {
	pin := s.registry.PinDirection(mapping.DirectionV1ToV2)
	if pin.Active == nil {
		return nil, status.Error(codes.Unavailable, "no active v1->v2 mapping")
	}
	if req.GetRecord() == nil {
		return nil, status.Error(codes.InvalidArgument, "record is required")
	}
	res, ferr := transform.V1ToV2(pin.Active.Spec, req.GetRecord(), req.GetSkipSignatureVerify())
	if ferr != nil {
		s.audit(ctx, pin, req.GetRecord().GetRequestId(), ferr, nil)
		return nil, failureToStatus(ferr)
	}
	s.audit(ctx, pin, req.GetRecord().GetRequestId(), nil, nil)
	return &gatewayv1.ConvertV1ToV2Response{
		Record:     res.Record,
		Provenance: s.provenance(pin, res.UnknownN, nil),
	}, nil
}

func (s *Server) ConvertV2ToV1(ctx context.Context, req *gatewayv1.ConvertV2ToV1Request) (*gatewayv1.ConvertV2ToV1Response, error) {
	pin := s.registry.PinDirection(mapping.DirectionV2ToV1)
	if pin.Active == nil {
		return nil, status.Error(codes.Unavailable, "no active v2->v1 mapping")
	}
	if req.GetRecord() == nil {
		return nil, status.Error(codes.InvalidArgument, "record is required")
	}
	res, ferr := transform.V2ToV1(pin.Active.Spec, req.GetRecord(), req.GetSkipSignatureVerify())
	if ferr != nil {
		s.audit(ctx, pin, req.GetRecord().GetTraceId(), ferr, nil)
		return nil, failureToStatus(ferr)
	}
	var carried *int32
	s.audit(ctx, pin, req.GetRecord().GetTraceId(), nil, res.Carried)
	if res.Carried != nil {
		c := *res.Carried
		carried = &c
	}
	return &gatewayv1.ConvertV2ToV1Response{
		Record:     res.Record,
		Provenance: s.provenance(pin, res.UnknownN, carried),
	}, nil
}

// streamJob types are concrete per direction (v1v2Job / v2v1Job below).

// ConvertStreamV1ToV2 runs an ordered, backpressured conversion stream.
func (s *Server) ConvertStreamV1ToV2(str gatewayv1.CompatGateway_ConvertStreamV1ToV2Server) error {
	ctx := str.Context()
	pin := s.registry.PinDirection(mapping.DirectionV1ToV2)
	if pin.Active == nil {
		return status.Error(codes.Unavailable, "no active v1->v2 mapping")
	}
	jobs := make(chan *v1v2Job, s.maxInFlight)
	return runOrderedStream(ctx, jobs,
		func() (uint64, string, *v1v2Job, error) {
			in, err := str.Recv()
			if err != nil {
				return 0, "", nil, err
			}
			j := &v1v2Job{seq: in.GetSeq(), req: in.GetItem()}
			return j.seq, j.req.GetRecord().GetRequestId(), j, nil
		},
		func(j *v1v2Job) *gatewayv1.StreamV2Response {
			r, ferr := transform.V1ToV2(pin.Active.Spec, j.req.GetRecord(), j.req.GetSkipSignatureVerify())
			if ferr != nil {
				s.audit(ctx, pin, j.req.GetRecord().GetRequestId(), ferr, nil)
				return &gatewayv1.StreamV2Response{Seq: j.seq, Outcome: &gatewayv1.StreamV2Response_Error{Error: toProtoErr(ferr, j.seq)}}
			}
			s.audit(ctx, pin, j.req.GetRecord().GetRequestId(), nil, nil)
			return &gatewayv1.StreamV2Response{Seq: j.seq, Outcome: &gatewayv1.StreamV2Response_Ok{
				Ok: &gatewayv1.ConvertV1ToV2Response{
					Record:     r.Record,
					Provenance: s.provenance(pin, r.UnknownN, nil),
				},
			}}
		},
		str.Send)
}

type v1v2Job struct {
	seq uint64
	req *gatewayv1.ConvertV1ToV2Request
}

type v2v1Job struct {
	seq uint64
	req *gatewayv1.ConvertV2ToV1Request
}

// ConvertStreamV2ToV1 runs an ordered, backpressured conversion stream.
func (s *Server) ConvertStreamV2ToV1(str gatewayv1.CompatGateway_ConvertStreamV2ToV1Server) error {
	ctx := str.Context()
	pin := s.registry.PinDirection(mapping.DirectionV2ToV1)
	if pin.Active == nil {
		return status.Error(codes.Unavailable, "no active v2->v1 mapping")
	}
	jobs := make(chan *v2v1Job, s.maxInFlight)
	return runOrderedStream(ctx, jobs,
		func() (uint64, string, *v2v1Job, error) {
			in, err := str.Recv()
			if err != nil {
				return 0, "", nil, err
			}
			j := &v2v1Job{seq: in.GetSeq(), req: in.GetItem()}
			return j.seq, j.req.GetRecord().GetTraceId(), j, nil
		},
		func(j *v2v1Job) *gatewayv1.StreamV1Response {
			r, ferr := transform.V2ToV1(pin.Active.Spec, j.req.GetRecord(), j.req.GetSkipSignatureVerify())
			if ferr != nil {
				s.audit(ctx, pin, j.req.GetRecord().GetTraceId(), ferr, nil)
				return &gatewayv1.StreamV1Response{Seq: j.seq, Outcome: &gatewayv1.StreamV1Response_Error{Error: toProtoErr(ferr, j.seq)}}
			}
			s.audit(ctx, pin, j.req.GetRecord().GetTraceId(), nil, r.Carried)
			var carried *int32
			if r.Carried != nil {
				c := *r.Carried
				carried = &c
			}
			return &gatewayv1.StreamV1Response{Seq: j.seq, Outcome: &gatewayv1.StreamV1Response_Ok{
				Ok: &gatewayv1.ConvertV2ToV1Response{
					Record:     r.Record,
					Provenance: s.provenance(pin, r.UnknownN, carried),
				},
			}}
		},
		str.Send)
}

func (s *Server) provenance(pin Pin, unknownN uint32, carried *int32) *gatewayv1.Provenance {
	sp := pin.Active.Spec
	p := &gatewayv1.Provenance{
		SourceVersion:     sp.SourceVersion,
		ConvertedVersion:  sp.TargetVersion,
		MappingName:       pin.Active.Name,
		MappingVersion:    pin.Active.Version,
		UnknownFieldsSeen: unknownN,
	}
	if carried != nil {
		c := *carried
		p.CarriedOriginalEnum = &c
	}
	return p
}

func (s *Server) audit(ctx context.Context, pin Pin, recID string, err error, carried *int32) {
	r := store.AuditRecord{
		Direction:      pin.Direction,
		MappingName:    pin.Active.Name,
		MappingVersion: pin.Active.Version,
		RecordID:       recID,
		OK:             err == nil,
		CarriedEnum:    carried,
	}
	if f, ok := err.(*mapping.Failure); ok {
		r.ErrorCode = string(f.Code)
		r.ErrorField = f.Field
	}
	// Audit failures must not take the RPC down; the conversion result is the
	// contract. Context errors are expected on shutdown/cancel; skip those.
	if ctx.Err() != nil {
		return
	}
	_ = s.store.InsertAudit(ctx, r)
}

func (s *Server) GetMapping(ctx context.Context, _ *gatewayv1.GetMappingRequest) (*gatewayv1.GetMappingResponse, error) {
	// Report both active mappings as separate messages is awkward; return v1->v2
	// snapshot. Administration tooling can query the DB for the full table.
	am := s.registry.Current(mapping.DirectionV1ToV2)
	if am == nil {
		return nil, status.Error(codes.Unavailable, "mapping not loaded")
	}
	return &gatewayv1.GetMappingResponse{
		Name: am.Name, Version: am.Version, Active: true, SpecJson: string(am.Spec.JSON()),
	}, nil
}

func (s *Server) ActivateMapping(ctx context.Context, req *gatewayv1.ActivateMappingRequest) (*gatewayv1.ActivateMappingResponse, error) {
	var am *store.ActiveMapping
	var err error
	if req.GetSpecJson() != "" {
		am, err = s.store.UpsertAndActivate(ctx, []byte(req.GetSpecJson()))
	} else if req.GetName() != "" {
		am, err = s.store.ActivateByName(ctx, req.GetName())
	} else {
		return nil, status.Error(codes.InvalidArgument, "name or spec_json is required")
	}
	if err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "activate mapping: %v", err)
	}
	if err := s.registry.Reload(ctx); err != nil {
		return nil, status.Errorf(codes.Internal, "reload mappings: %v", err)
	}
	return &gatewayv1.ActivateMappingResponse{Name: am.Name, Version: am.Version}, nil
}

func failureToStatus(err error) error {
	f, ok := err.(*mapping.Failure)
	if !ok {
		return status.Errorf(codes.Internal, "%v", err)
	}
	p := toProto(f, 0)
	st := status.New(codeFor(f.Code), f.Message)
	st, _ = st.WithDetails(p)
	return st.Err()
}

func codeFor(c mapping.FailureCode) codes.Code {
	switch c {
	case mapping.FailEnumOverflow:
		return codes.InvalidArgument
	case mapping.FailIntegerOverflow:
		return codes.OutOfRange
	case mapping.FailRoundingRequired:
		return codes.FailedPrecondition
	case mapping.FailSignatureInvalid:
		return codes.Unauthenticated
	default:
		return codes.Internal
	}
}

func toProto(f *mapping.Failure, seq uint64) *gatewayv1.ConversionError {
	p := &gatewayv1.ConversionError{
		Field:    f.Field,
		Message:  f.Message,
		RecordId: f.RecordID,
		Seq:      seq,
	}
	switch f.Code {
	case mapping.FailEnumOverflow:
		p.Code = gatewayv1.ConversionError_ENUM_UNMAPPABLE
	case mapping.FailIntegerOverflow:
		p.Code = gatewayv1.ConversionError_INTEGER_OVERFLOW
	case mapping.FailRoundingRequired:
		p.Code = gatewayv1.ConversionError_ROUNDING_REQUIRED
	case mapping.FailSignatureInvalid:
		p.Code = gatewayv1.ConversionError_SIGNATURE_INVALID
	default:
		p.Code = gatewayv1.ConversionError_CODE_UNSPECIFIED
	}
	if f.OriginalEnum != nil {
		v := int32(*f.OriginalEnum)
		p.OriginalEnum = &v
	}
	return p
}

// toProtoErr converts a stream-item failure into a locatable wire error. A
// non-mapping failure should never occur in conversion, but is reported rather
// than masked.
func toProtoErr(err error, seq uint64) *gatewayv1.ConversionError {
	if f, ok := err.(*mapping.Failure); ok {
		return toProto(f, seq)
	}
	return &gatewayv1.ConversionError{
		Code:    gatewayv1.ConversionError_CODE_UNSPECIFIED,
		Message: err.Error(),
		Seq:     seq,
	}
}

// generic ordered-stream engine, parameterised by job type and response type.
func runOrderedStream[J any, R any](
	ctx context.Context,
	jobs chan J,
	recv func() (seq uint64, recID string, job J, err error),
	handle func(J) R,
	send func(R) error,
) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	recvDone := make(chan error, 1)

	// Receiver: fills the bounded queue. Sends block when the queue is full,
	// which propagates backpressure to the client's flow-control window.
	go func() {
		for {
			seq, _, job, err := recv()
			if err != nil {
				recvDone <- err
				return
			}
			select {
			case jobs <- job:
			case <-ctx.Done():
				recvDone <- ctx.Err()
				return
			}
			_ = seq
		}
	}()

	// Single worker keeps responses ordered. Conversion is CPU-bound; one
	// worker preserves sequence order without reordering buffers.
	for {
		select {
		case <-ctx.Done():
			// Cancellation stops conversion immediately. Drain nothing.
			return ctx.Err()
		case j := <-jobs:
			if err := send(handle(j)); err != nil {
				// Half-stream failure: client stopped reading (or broke the
				// stream). Abort at once; context cancels the receiver too.
				return err
			}
		case err := <-recvDone:
			if errors.Is(err, io.EOF) {
				// Drain anything still queued, then finish cleanly.
				close(jobs)
				for j := range jobs {
					if err := send(handle(j)); err != nil {
						return err
					}
				}
				return nil
			}
			return err
		}
	}
}
