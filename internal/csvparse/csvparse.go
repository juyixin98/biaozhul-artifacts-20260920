// Package csvparse parses and validates CostLens billing CSV batches.
//
// Expected header (exact, order-sensitive):
//
//	resource_id,service,date,currency,amount
//
// date is YYYY-MM-DD, currency an ISO-4217-style 3-letter uppercase code,
// amount an exact fixed-point decimal with at most 6 fractional digits.
// MaxRows is enforced: the 5001st data row is an error.
package csvparse

import (
	"encoding/csv"
	"fmt"
	"io"
	"regexp"
	"strings"
	"time"

	"github.com/shopspring/decimal"
)

const MaxRows = 5000

var (
	header         = []string{"resource_id", "service", "date", "currency", "amount"}
	currencyRegexp = regexp.MustCompile(`^[A-Z]{3}$`)
	// Explicit sign, digits, optional fraction. No exponent, no NaN/Inf.
	amountRegexp = regexp.MustCompile(`^-?[0-9]+(\.[0-9]{1,6})?$`)
)

// Row is one validated CSV data row. Line is the 1-based file line number
// (data rows begin on line 2).
type Row struct {
	Line       int
	ResourceID string
	Service    string
	Date       time.Time
	Currency   string
	Amount     decimal.Decimal
}

// RowError points at one offending input line.
type RowError struct {
	Line    int    `json:"line"`
	Field   string `json:"field,omitempty"`
	Message string `json:"message"`
}

func (e *RowError) Error() string {
	if e.Field != "" {
		return fmt.Sprintf("line %d: %s: %s", e.Line, e.Field, e.Message)
	}
	return fmt.Sprintf("line %d: %s", e.Line, e.Message)
}

// ParseError aggregates every row validation error found in a batch.
type ParseError struct {
	Errors []RowError
}

func (e *ParseError) Error() string {
	if len(e.Errors) == 1 {
		return e.Errors[0].Error()
	}
	return fmt.Sprintf("%d validation errors, first: %v", len(e.Errors), e.Errors[0])
}

// Parse reads the whole document and returns validated rows. It never accepts
// a partial batch: any malformed row yields a ParseError.
func Parse(r io.Reader) ([]Row, error) {
	cr := csv.NewReader(r)
	cr.FieldsPerRecord = -1 // checked manually for a clearer error
	cr.TrimLeadingSpace = true

	head, err := cr.Read()
	if err == io.EOF {
		return nil, &ParseError{Errors: []RowError{{Line: 1, Message: "empty file: expected header row"}}}
	}
	if err != nil {
		return nil, &ParseError{Errors: []RowError{{Line: 1, Message: "cannot read header: " + err.Error()}}}
	}
	headLine := 1
	// encoding/csv strips a trailing \r; trim defensively on each field.
	for i := range head {
		head[i] = strings.TrimSpace(head[i])
	}
	if !equalFields(head, header) {
		return nil, &ParseError{Errors: []RowError{{
			Line:    headLine,
			Message: fmt.Sprintf("invalid header: expected %s, got %s", strings.Join(header, ","), strings.Join(head, ",")),
		}}}
	}

	var rows []Row
	var errs []RowError
	dataRow := 0
	for {
		rec, err := cr.Read()
		if err == io.EOF {
			break
		}
		line := dataRow + 2 // header is line 1
		if err != nil {
			errs = append(errs, RowError{Line: line, Message: "malformed CSV: " + err.Error()})
			// A field-count error still desynchronizes parsing; stop collecting.
			return nil, &ParseError{Errors: errs}
		}
		dataRow++
		line = dataRow + 1
		if dataRow > MaxRows {
			errs = append(errs, RowError{Line: line, Message: fmt.Sprintf("batch exceeds %d rows", MaxRows)})
			return nil, &ParseError{Errors: errs}
		}
		for i := range rec {
			rec[i] = strings.TrimSpace(rec[i])
		}
		if len(rec) != len(header) {
			errs = append(errs, RowError{Line: line, Message: fmt.Sprintf("expected %d columns, got %d", len(header), len(rec))})
			continue
		}

		row := Row{Line: line}
		ok := true
		if rec[0] == "" {
			errs = append(errs, RowError{Line: line, Field: "resource_id", Message: "must not be empty"})
			ok = false
		} else {
			row.ResourceID = rec[0]
		}
		if rec[1] == "" {
			errs = append(errs, RowError{Line: line, Field: "service", Message: "must not be empty"})
			ok = false
		} else {
			row.Service = rec[1]
		}
		d, perr := time.Parse("2006-01-02", rec[2])
		if rec[2] == "" || perr != nil {
			errs = append(errs, RowError{Line: line, Field: "date", Message: "must be YYYY-MM-DD"})
			ok = false
		} else {
			row.Date = d
		}
		if !currencyRegexp.MatchString(rec[3]) {
			errs = append(errs, RowError{Line: line, Field: "currency", Message: "must be a 3-letter uppercase ISO code"})
			ok = false
		} else {
			row.Currency = rec[3]
		}
		if !amountRegexp.MatchString(rec[4]) {
			errs = append(errs, RowError{Line: line, Field: "amount", Message: "must be a fixed-point decimal with at most 6 fractional digits"})
			ok = false
		} else {
			a, aerr := decimal.NewFromString(rec[4])
			if aerr != nil {
				errs = append(errs, RowError{Line: line, Field: "amount", Message: aerr.Error()})
				ok = false
			} else {
				row.Amount = a
			}
		}
		if ok {
			rows = append(rows, row)
		}
	}
	if len(errs) > 0 {
		return nil, &ParseError{Errors: errs}
	}
	if len(rows) == 0 {
		return nil, &ParseError{Errors: []RowError{{Line: 1, Message: "batch contains no data rows"}}}
	}
	return rows, nil
}

func equalFields(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
