// Package csvio parses bill CSV uploads.
//
// Columns (header row, order fixed):
//
//	account_code,resource_code,service,cost_date,currency,amount
//
// At most 5000 data rows per batch. Every error reports the 1-based file line
// (the header is line 1, the first data row is line 2).
package csvio

import (
	"encoding/csv"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"costlens/internal/money"

	"github.com/shopspring/decimal"
)

const MaxRows = 5000

var Header = []string{"account_code", "resource_code", "service", "cost_date", "currency", "amount"}

type Row struct {
	Line        int
	AccountCode string
	ResourceID  string // resolved later by service; carries resource_code during parse
	Service     string
	CostDate    time.Time
	Currency    string
	Amount      decimal.Decimal
}

// ParseError references the exact file line and field.
type ParseError struct {
	Line    int
	Field   string
	Message string
}

func (e *ParseError) Error() string {
	if e.Field != "" {
		return fmt.Sprintf("line %d: %s: %s", e.Line, e.Field, e.Message)
	}
	return fmt.Sprintf("line %d: %s", e.Line, e.Message)
}

// Parse reads the full upload and returns validated rows. It never partially
// succeeds: the first invalid row aborts parsing with a line-numbered error.
func Parse(r io.Reader) ([]Row, error) {
	cr := csv.NewReader(r)
	cr.FieldsPerRecord = len(Header)
	cr.TrimLeadingSpace = true
	cr.ReuseRecord = false

	header, err := cr.Read()
	if err != nil {
		return nil, &ParseError{Line: 1, Message: "cannot read header: " + err.Error()}
	}
	for i, want := range Header {
		if strings.TrimSpace(header[i]) != want {
			return nil, &ParseError{Line: 1, Field: "header",
				Message: fmt.Sprintf("column %d must be %q, got %q", i+1, want, strings.TrimSpace(header[i]))}
		}
	}

	rows := make([]Row, 0, 256)
	for {
		rec, err := cr.Read()
		if errors.Is(err, io.EOF) {
			break
		}
		line := len(rows) + 2
		if err != nil {
			return nil, &ParseError{Line: line, Message: err.Error()}
		}
		row, perr := parseRecord(line, rec)
		if perr != nil {
			return nil, perr
		}
		rows = append(rows, row)
		if len(rows) > MaxRows {
			return nil, &ParseError{Line: len(rows) + 1,
				Message: fmt.Sprintf("batch exceeds %d data rows", MaxRows)}
		}
	}
	if len(rows) == 0 {
		return nil, &ParseError{Line: 1, Message: "file contains no data rows"}
	}
	return rows, nil
}

func parseRecord(line int, rec []string) (Row, *ParseError) {
	get := func(i int, name string) (string, *ParseError) {
		v := strings.TrimSpace(rec[i])
		if v == "" {
			return "", &ParseError{Line: line, Field: name, Message: "must not be empty"}
		}
		return v, nil
	}

	account, e := get(0, "account_code")
	if e != nil {
		return Row{}, e
	}
	resource, e := get(1, "resource_code")
	if e != nil {
		return Row{}, e
	}
	service, e := get(2, "service")
	if e != nil {
		return Row{}, e
	}
	dateStr, e := get(3, "cost_date")
	if e != nil {
		return Row{}, e
	}
	currency, e := get(4, "currency")
	if e != nil {
		return Row{}, e
	}
	amountStr, e := get(5, "amount")
	if e != nil {
		return Row{}, e
	}

	d, err := time.Parse("2006-01-02", dateStr)
	if err != nil {
		return Row{}, &ParseError{Line: line, Field: "cost_date",
			Message: "must be YYYY-MM-DD: " + dateStr}
	}
	if err := money.ValidateCurrency(currency); err != nil {
		return Row{}, &ParseError{Line: line, Field: "currency", Message: err.Error()}
	}
	amount, err := money.ParseAmount(amountStr)
	if err != nil {
		return Row{}, &ParseError{Line: line, Field: "amount", Message: err.Error()}
	}

	return Row{
		Line:        line,
		AccountCode: account,
		ResourceID:  resource,
		Service:     service,
		CostDate:    d,
		Currency:    currency,
		Amount:      amount,
	}, nil
}
