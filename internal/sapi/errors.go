package sapi

import (
	"errors"
	"fmt"
)

// Kind says what a failure means for the caller (retry, re-login, give up).
type Kind int

const (
	KindAPI        Kind = iota // O2 answered with an error code: not retried blindly
	KindSession                // session expired/invalidated (401 + HTML login page)
	KindKeyRotated             // SEC-1003: validation key rotated; the new one is in NewKey
	KindEdge                   // empty 401/403 (CloudFront, or X-deviceid not the session's)
	KindTransient              // network, timeout, 408/429/5xx: a retry may work
	KindNotFound
)

func (k Kind) String() string {
	return [...]string{"api", "session", "key-rotated", "edge", "transient", "not-found"}[k]
}

type Error struct {
	Op     string
	Status int
	Code   string // SAPI error code (SEC-1003, MED-1017, FOL-1036...)
	Msg    string
	Kind   Kind
	NewKey string
	Err    error
}

func (e *Error) Error() string {
	s := e.Op
	if e.Status != 0 {
		s += fmt.Sprintf(" -> HTTP %d", e.Status)
	}
	if e.Code != "" {
		s += " " + e.Code
	}
	if e.Msg != "" {
		s += ": " + e.Msg
	}
	if e.Err != nil {
		s += ": " + e.Err.Error()
	}
	return s
}

func (e *Error) Unwrap() error { return e.Err }

func asErr(err error) *Error {
	var e *Error
	if errors.As(err, &e) {
		return e
	}
	return nil
}

func KindOf(err error) (Kind, bool) {
	if e := asErr(err); e != nil {
		return e.Kind, true
	}
	return 0, false
}

func CodeOf(err error) string {
	if e := asErr(err); e != nil {
		return e.Code
	}
	return ""
}

func IsSession(err error) bool { k, ok := KindOf(err); return ok && k == KindSession }
func IsTransient(err error) bool {
	k, ok := KindOf(err)
	return ok && (k == KindTransient || k == KindEdge)
}

// Errors with a fixed meaning for the WebDAV layer.
var (
	ErrTooLarge       = errors.New("O2 no admite ficheros tan grandes")
	ErrUnsupported    = errors.New("O2 no admite ese contenido para ese tipo de fichero")
	ErrFolderTooBig   = errors.New("la carpeta tiene demasiados elementos para O2 (FOL-1036)")
	ErrEmptyOverwrite = errors.New("O2 no admite sobrescribir un fichero con 0 bytes")
	ErrValidating     = errors.New("O2 aún está procesando el fichero (MED-1017)")
)

// mapCode turns well-known SAPI codes into the sentinel errors above.
func mapCode(e *Error) error {
	switch e.Code {
	case "MED-1020", "MED-1048":
		return fmt.Errorf("%w: %w", ErrTooLarge, e)
	case "MED-1050":
		return fmt.Errorf("%w: %w", ErrUnsupported, e)
	case "FOL-1036":
		return fmt.Errorf("%w: %w", ErrFolderTooBig, e)
	case "MED-1017":
		return fmt.Errorf("%w: %w", ErrValidating, e)
	}
	return e
}
