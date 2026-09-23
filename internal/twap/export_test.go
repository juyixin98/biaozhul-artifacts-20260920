package twap

import "math/big"

// TESTRatToFixed exposes the internal fixed-point renderer to external tests.
var TESTRatToFixed = func(num, den *big.Int, digits int) string {
	return ratToFixed(num, den, digits)
}
