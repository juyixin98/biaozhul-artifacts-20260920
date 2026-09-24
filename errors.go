package main

import "strconv"

// JSON-RPC 2.0 error codes. See https://www.jsonrpc.org/specification#error_object
const (
	codeParseError     = -32700
	codeInvalidRequest = -32600
	codeMethodNotFound = -32601
	codeInvalidParams  = -32602
	codeInternalError  = -32603
	// -32000..-32099 are reserved for server-defined errors.
	codeServerError = -32000
)

// rpcError is a JSON-RPC 2.0 error object. When a Handler returns an *rpcError,
// its code/message/data are forwarded to the client verbatim; any other error
// becomes a generic -32603 Internal error so internal details never leak.
type rpcError struct {
	Code    int         `json:"code"`
	Message string      `json:"message"`
	Data    interface{} `json:"data,omitempty"`
}

func (e *rpcError) Error() string {
	if e == nil {
		return ""
	}
	return "jsonrpc error " + strconv.Itoa(e.Code) + ": " + e.Message
}

func errParseError() *rpcError {
	return &rpcError{Code: codeParseError, Message: "Parse error"}
}

func errInvalidRequest(data interface{}) *rpcError {
	return &rpcError{Code: codeInvalidRequest, Message: "Invalid Request", Data: data}
}

func errMethodNotFound(method string) *rpcError {
	return &rpcError{Code: codeMethodNotFound, Message: "Method not found", Data: method}
}

func errInvalidParams(msg string) *rpcError {
	return &rpcError{Code: codeInvalidParams, Message: "Invalid params", Data: msg}
}

func errInternal() *rpcError {
	return &rpcError{Code: codeInternalError, Message: "Internal error"}
}
