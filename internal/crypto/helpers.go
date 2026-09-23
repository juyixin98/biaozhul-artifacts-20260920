package crypto

import (
	"encoding/json"
	"math"
	"strconv"
)

func writeJSONString(b *[]byte, s string) {
	// json.Marshal produces valid compact JSON string escaping.
	e, err := json.Marshal(s)
	if err != nil {
		// Only fails for invalid UTF-8 in older paths; replace to keep total.
		e, _ = json.Marshal(string([]rune(s)))
	}
	*b = append(*b, e...)
}

func appendFloat(b []byte, f float64) []byte {
	switch {
	case math.IsNaN(f) || math.IsInf(f, 0):
		// JSON has no NaN/Infinity; encode as null so encoding stays valid.
		return append(b, "null"...)
	default:
		return strconv.AppendFloat(b, f, 'g', -1, 64)
	}
}

func appendDecInt(b []byte, i int64) []byte {
	return strconv.AppendInt(b, i, 10)
}
