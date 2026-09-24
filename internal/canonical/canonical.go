// Package canonical implements the small, explicitly specified canonical JSON
// encoding used by build attestations.
//
// It is NOT RFC 8785 (JCS). The rules are intentionally minimal and fixed so
// that an attestation signer and verifier can never disagree on the signed
// bytes:
//
//   - Objects have unique keys, serialized in ascending byte-wise UTF-8 order.
//   - Duplicate keys, BOMs, invalid UTF-8 and trailing data are rejected.
//   - Strings use minimal JSON escaping (", \ and control bytes only); "/" and
//     non-ASCII characters are never escaped.
//   - Numbers may only be integers in canonical literal form: no 1.0, no 1e3,
//     no leading zeros and no -0.
//   - Arrays preserve order; there is no insignificant whitespace.
package canonical

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strconv"
	"unicode/utf8"
)

// integerLiteral matches canonical integer JSON number literals only.
var integerLiteral = regexp.MustCompile(`\A-?(?:0|[1-9][0-9]*)\z`)

// DuplicateKeyError reports a repeated key inside a single JSON object.
type DuplicateKeyError struct{ Key string }

func (e *DuplicateKeyError) Error() string {
	return fmt.Sprintf("canonical: duplicate JSON key %q", e.Key)
}

// Parse strictly decodes data into map[string]any / []any / string /
// json.Number / bool / nil, rejecting duplicate keys and non-canonical numbers.
func Parse(data []byte) (any, error) {
	if len(bytes.TrimSpace(data)) == 0 {
		return nil, errors.New("canonical: empty JSON document")
	}
	if bytes.HasPrefix(data, []byte{0xEF, 0xBB, 0xBF}) {
		return nil, errors.New("canonical: UTF-8 BOM is not allowed")
	}
	if !utf8.Valid(data) {
		return nil, errors.New("canonical: invalid UTF-8")
	}

	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()

	v, err := decodeValue(dec)
	if err != nil {
		return nil, err
	}
	// A tokenizer-based decoder tolerates trailing tokens; forbid them.
	if tok, err := dec.Token(); err != io.EOF {
		if err == nil {
			return nil, fmt.Errorf("canonical: trailing data after JSON value (%v)", tok)
		}
		return nil, fmt.Errorf("canonical: trailing data after JSON value")
	}
	return v, nil
}

func decodeValue(dec *json.Decoder) (any, error) {
	tok, err := dec.Token()
	if err != nil {
		return nil, fmt.Errorf("canonical: invalid JSON: %w", err)
	}

	switch t := tok.(type) {
	case json.Delim:
		switch t {
		case '{':
			m := map[string]any{}
			for dec.More() {
				keyTok, err := dec.Token()
				if err != nil {
					return nil, fmt.Errorf("canonical: invalid object key: %w", err)
				}
				key, ok := keyTok.(string)
				if !ok {
					return nil, errors.New("canonical: object key is not a string")
				}
				if _, dup := m[key]; dup {
					return nil, &DuplicateKeyError{Key: key}
				}
				child, err := decodeValue(dec)
				if err != nil {
					return nil, err
				}
				m[key] = child
			}
			if _, err := dec.Token(); err != nil { // consume '}'
				return nil, fmt.Errorf("canonical: unterminated object: %w", err)
			}
			return m, nil
		case '[':
			a := []any{}
			for dec.More() {
				child, err := decodeValue(dec)
				if err != nil {
					return nil, err
				}
				a = append(a, child)
			}
			if _, err := dec.Token(); err != nil { // consume ']'
				return nil, fmt.Errorf("canonical: unterminated array: %w", err)
			}
			return a, nil
		default:
			return nil, fmt.Errorf("canonical: unexpected delimiter %q", t)
		}
	case json.Number:
		if string(t) == "-0" || !integerLiteral.MatchString(string(t)) {
			return nil, fmt.Errorf("canonical: number %q is not a canonical integer (no fractions/exponents/leading zero/-0)", t)
		}
		return t, nil
	default:
		// string, bool, nil arrive as concrete types.
		return tok, nil
	}
}

// Marshal re-serializes a value produced (or accepted) by Parse into canonical
// JSON bytes.
func Marshal(v any) ([]byte, error) {
	var b bytes.Buffer
	if err := encode(&b, v); err != nil {
		return nil, err
	}
	return b.Bytes(), nil
}

func encode(b *bytes.Buffer, v any) error {
	switch t := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := encode(b, k); err != nil {
				return err
			}
			b.WriteByte(':')
			if err := encode(b, t[k]); err != nil {
				return err
			}
		}
		b.WriteByte('}')
	case []any:
		b.WriteByte('[')
		for i, child := range t {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := encode(b, child); err != nil {
				return err
			}
		}
		b.WriteByte(']')
	case string:
		if !utf8.ValidString(t) {
			return errors.New("canonical: string contains invalid UTF-8")
		}
		writeString(b, t)
	case json.Number:
		if string(t) == "-0" || !integerLiteral.MatchString(string(t)) {
			return fmt.Errorf("canonical: number %q is not a canonical integer", t)
		}
		b.WriteString(string(t))
	case int:
		b.WriteString(strconv.Itoa(t))
	case int64:
		b.WriteString(strconv.FormatInt(t, 10))
	case bool:
		if t {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case nil:
		b.WriteString("null")
	default:
		return fmt.Errorf("canonical: unsupported value type %T", v)
	}
	return nil
}

func writeString(b *bytes.Buffer, s string) {
	b.WriteByte('"')
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch c {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			if c < 0x20 {
				fmt.Fprintf(b, `\u%04x`, c)
			} else {
				b.WriteByte(c) // UTF-8 bytes pass through unchanged, including >= 0x80
			}
		}
	}
	b.WriteByte('"')
}
