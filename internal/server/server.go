// Package server is the Modbus/TCP gateway: it accepts client connections,
// validates MBAP framing and dispatches FC03 (read holding registers) and
// FC10 (write multiple registers) against the persistent store.
package server

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"sync"
	"time"

	"modbus-receipt/internal/protocol"
	"modbus-receipt/internal/storage"
)

// Config configures the Server.
type Config struct {
	ListenAddr  string
	Store       *storage.Store
	ReadTimeout time.Duration // idle/read deadline per frame; 0 = no deadline
}

// Server is a running Modbus/TCP listener.
type Server struct {
	cfg Config
	log *log.Logger

	mu       sync.Mutex
	ln       net.Listener
	closed   chan struct{}
	shutdown sync.Once
	wg       sync.WaitGroup
	midWrite func() // test hook, applied to every store.Write

	connsMu sync.Mutex
	conns   map[net.Conn]struct{} // live connections; force-closed on Shutdown
}

// New creates a server. Call Serve to start accepting connections.
func New(cfg Config, logger *log.Logger) *Server {
	if logger == nil {
		logger = log.Default()
	}
	return &Server{
		cfg:    cfg,
		log:    logger,
		closed: make(chan struct{}),
		conns:  make(map[net.Conn]struct{}),
	}
}

// SetMidWriteHook installs a test hook invoked inside Store.Write after the
// batch's registers are UPSERTed but before commit. Never use in production.
func (s *Server) SetMidWriteHook(h func()) {
	s.mu.Lock()
	s.midWrite = h
	s.mu.Unlock()
}

// Serve starts listening and blocks until the listener is closed (Shutdown).
func (s *Server) Serve(ctx context.Context) error {
	ln, err := net.Listen("tcp", s.cfg.ListenAddr)
	if err != nil {
		return fmt.Errorf("server: listen %s: %w", s.cfg.ListenAddr, err)
	}
	s.mu.Lock()
	s.ln = ln
	s.mu.Unlock()
	s.log.Printf("modbus gateway listening on %s (bank=%d registers)",
		ln.Addr(), s.cfg.Store.BankSize())

	for {
		conn, err := ln.Accept()
		if err != nil {
			select {
			case <-s.closed:
				s.wg.Wait()
				return nil
			default:
				return fmt.Errorf("server: accept: %w", err)
			}
		}
		s.wg.Add(1)
		go func(c net.Conn) {
			defer s.wg.Done()
			defer s.unregisterConn(c)
			s.registerConn(c)
			s.handleConn(ctx, c)
		}(conn)
	}
}

// Addr reports the concrete listener address (useful with ":0" in tests).
func (s *Server) Addr() net.Addr {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ln == nil {
		return nil
	}
	return s.ln.Addr()
}

// Shutdown stops accepting new connections, closes all live connections so
// blocked handlers unwind, and waits for them to finish. Safe to call once.
func (s *Server) Shutdown() {
	s.shutdown.Do(func() {
		close(s.closed)

		s.mu.Lock()
		ln := s.ln
		s.mu.Unlock()
		if ln != nil {
			_ = ln.Close()
		}

		// Force handlers blocked on ReadFrame (idle keep-alive connections)
		// to terminate; otherwise Serve could never return on shutdown.
		s.connsMu.Lock()
		for c := range s.conns {
			_ = c.Close()
		}
		s.connsMu.Unlock()

		s.wg.Wait()
	})
}

func (s *Server) registerConn(c net.Conn) {
	s.connsMu.Lock()
	s.conns[c] = struct{}{}
	s.connsMu.Unlock()
}

func (s *Server) unregisterConn(c net.Conn) {
	s.connsMu.Lock()
	delete(s.conns, c)
	s.connsMu.Unlock()
}

func (s *Server) handleConn(ctx context.Context, conn net.Conn) {
	remote := conn.RemoteAddr().String()
	s.log.Printf("connect %s", remote)
	defer func() {
		conn.Close()
		s.log.Printf("disconnect %s", remote)
	}()

	if s.cfg.ReadTimeout > 0 {
		_ = conn.SetReadDeadline(time.Now().Add(s.cfg.ReadTimeout))
	}

	for {
		frame, err := protocol.ReadFrame(conn)
		if err != nil {
			// Normal end: FIN from peer. Everything else is a framing/transport
			// failure. Per the Modbus/TCP implementation guide the connection
			// simply terminates — there is no server-side retry, rollback or
			// idempotency replay; a committed FC10 stays committed (see README).
			if errors.Is(err, io.EOF) {
				return
			}
			if errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, protocol.ErrTruncated) {
				s.log.Printf("close %s: client left mid-frame (%v)", remote, err)
			} else {
				s.log.Printf("close %s: framing error: %v", remote, err)
			}
			return
		}

		respPDU := s.dispatch(ctx, conn, frame)
		resp := protocol.Frame{
			TransactionID: frame.TransactionID, // echoed verbatim
			ProtocolID:    protocol.ProtocolIDModbus,
			UnitID:        frame.UnitID, // echoed verbatim
			PDU:           respPDU,
		}
		if _, err := conn.Write(resp.Encode()); err != nil {
			// Response lost: peer gone or reset. The write already committed
			// if it was FC10 — TCP delivery is not a Modbus transaction.
			s.log.Printf("close %s: response write failed: %v", remote, err)
			return
		}

		if s.cfg.ReadTimeout > 0 {
			_ = conn.SetReadDeadline(time.Now().Add(s.cfg.ReadTimeout))
		}
	}
}

func (s *Server) dispatch(ctx context.Context, conn net.Conn, req protocol.Frame) []byte {
	fc := req.FunctionCode()

	// MBAP semantics (Implementation Guide §3.1.1 / §2):
	//  - Protocol Identifier must be 0 for Modbus; anything else is a bad
	//    request. The spec defines no exception for MBAP-level errors, so we
	//    answer Illegal Data Value on the same transaction and close after
	//    responding (documented server policy; client must treat it as fatal).
	if req.ProtocolID != protocol.ProtocolIDModbus {
		return protocol.ExceptionResponse(fc, protocol.ExcIllegalDataValue)
	}
	// Unit Identifier: this server is configured for explicit unit IDs
	// (default 1). Unknown unit -> exception 0x0B (Gateway Path Unavailable),
	// matching a serial gateway whose downstream unit is absent.
	if !s.cfg.Store.UnitAllowed(req.UnitID) {
		return protocol.ExceptionResponse(fc, protocol.ExcGatewayPathUnavailable)
	}

	switch fc {
	case protocol.FuncReadHoldingRegisters:
		return s.handleRead(ctx, req)
	case protocol.FuncWriteMultipleRegisters:
		return s.handleWrite(ctx, conn, req)
	default:
		// Function code is the only explicitly listed supported set;
		// everything else is 0x01 Illegal Function.
		return protocol.ExceptionResponse(fc, protocol.ExcIllegalFunction)
	}
}

func (s *Server) handleRead(ctx context.Context, req protocol.Frame) []byte {
	r, exc, ok := protocol.ParseReadHoldingRegistersRequest(req.PDU)
	if !ok {
		return protocol.ExceptionResponse(protocol.FuncReadHoldingRegisters, exc)
	}
	values, err := s.cfg.Store.Read(ctx, r.StartAddress, r.Quantity)
	if err != nil {
		if errors.Is(err, storage.ErrOutOfRange) {
			return protocol.ExceptionResponse(protocol.FuncReadHoldingRegisters,
				protocol.ExcIllegalDataAddress)
		}
		s.log.Printf("read error txn=%d: %v", req.TransactionID, err)
		return protocol.ExceptionResponse(protocol.FuncReadHoldingRegisters,
			protocol.ExcServerDeviceFailure)
	}
	return protocol.EncodeReadHoldingRegistersResponse(values)
}

func (s *Server) handleWrite(ctx context.Context, conn net.Conn, req protocol.Frame) []byte {
	w, exc, ok := protocol.ParseWriteMultipleRegistersRequest(req.PDU)
	if !ok {
		return protocol.ExceptionResponse(protocol.FuncWriteMultipleRegisters, exc)
	}

	s.mu.Lock()
	hook := s.midWrite
	s.mu.Unlock()

	// Atomic batch: the store range-checks every register first; if even one
	// is out of range the ENTIRE batch fails (0x02) and nothing changes.
	res, err := s.cfg.Store.Write(ctx,
		req.TransactionID, req.UnitID, w.StartAddress, w.Values,
		conn.RemoteAddr().String(), time.Now(), hook)
	if err != nil {
		if errors.Is(err, storage.ErrOutOfRange) {
			return protocol.ExceptionResponse(protocol.FuncWriteMultipleRegisters,
				protocol.ExcIllegalDataAddress)
		}
		s.log.Printf("write error txn=%d: %v", req.TransactionID, err)
		return protocol.ExceptionResponse(protocol.FuncWriteMultipleRegisters,
			protocol.ExcServerDeviceFailure)
	}

	r := res.Receipt
	s.log.Printf("write ok txn=%d unit=%d addr=%d qty=%d receipt#%d hash=%s",
		r.TransactionID, r.UnitID, r.StartAddress, r.Quantity, r.Seq, shortHash(r.Hash))
	return protocol.EncodeWriteMultipleRegistersResponse(w.StartAddress, w.Quantity())
}

func shortHash(h string) string {
	if len(h) <= 12 {
		return h
	}
	return h[:12] + "…"
}
