package service

import (
	"errors"
	"fmt"
)

// Kind 是业务错误类别，API 层据此映射 HTTP 状态码。
type Kind string

const (
	KindValidation   Kind = "validation"   // 400
	KindUnauthorized Kind = "unauthorized" // 401
	KindForbidden    Kind = "forbidden"    // 403
	KindNotFound     Kind = "not_found"    // 404
	KindConflict     Kind = "conflict"     // 409
	KindFileChanged  Kind = "file_changed" // 409：读取期间文件变化
	KindInternal     Kind = "internal"     // 500
)

// Error 是携带类别的服务错误。
type Error struct {
	Kind Kind
	Msg  string
}

func (e *Error) Error() string { return string(e.Kind) + ": " + e.Msg }

func kindErr(kind Kind, format string, args ...any) error {
	return &Error{Kind: kind, Msg: fmt.Sprintf(format, args...)}
}

// AsError 尝试从 error 中取出 *Error，取不到时包装为内部错误。
func AsError(err error) *Error {
	var e *Error
	if errors.As(err, &e) {
		return e
	}
	return &Error{Kind: KindInternal, Msg: err.Error()}
}
