package models

import (
	"database/sql/driver"
	"encoding/json"
	"errors"
)

// StringArray maps a JSONB column to/from []string (pgx returns raw bytes).
type StringArray []string

func (a *StringArray) Scan(src any) error {
	switch v := src.(type) {
	case []byte:
		return json.Unmarshal(v, (*[]string)(a))
	case string:
		return json.Unmarshal([]byte(v), (*[]string)(a))
	case nil:
		*a = nil
		return nil
	default:
		return errors.New("StringArray: unsupported scan source")
	}
}

func (a StringArray) Value() (driver.Value, error) {
	if a == nil {
		return nil, nil
	}
	return json.Marshal([]string(a))
}
