package pgtypex

import (
	"database/sql/driver"
	"encoding/json"
	"errors"
)

// StringList is a JSONB column holding a JSON array of strings. sqlc maps
// jsonb to []byte (which the encoding/json package emits base64-encoded);
// this type makes the wire format a normal JSON array such as
// ["evilco","spamword"].
type StringList []string

// Scan implements sql.Scanner for the JSONB payload pgx hands us.
func (s *StringList) Scan(src any) error {
	if src == nil {
		*s = nil
		return nil
	}
	switch v := src.(type) {
	case []byte:
		if len(v) == 0 {
			*s = nil
			return nil
		}
		return json.Unmarshal(v, s)
	case string:
		if v == "" {
			*s = nil
			return nil
		}
		return json.Unmarshal([]byte(v), s)
	default:
		return errors.New("StringList: unsupported scan source")
	}
}

// Value satisfies driver.Valuer for write paths.
func (s StringList) Value() (driver.Value, error) {
	if s == nil {
		return []byte("[]"), nil
	}
	return json.Marshal(s)
}

// MarshalJSON emits a real JSON array (an empty slice serializes as []).
func (s StringList) MarshalJSON() ([]byte, error) {
	if s == nil {
		return []byte("[]"), nil
	}
	return json.Marshal([]string(s))
}

// UnmarshalJSON accepts a JSON array.
func (s *StringList) UnmarshalJSON(b []byte) error {
	var v []string
	if err := json.Unmarshal(b, &v); err != nil {
		return err
	}
	*s = v
	return nil
}
