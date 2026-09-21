package service

import "strings"

// Masking helpers. Sensitive data (API secrets, card numbers) is only ever
// returned in masked form; the raw secret is shown exactly once at creation.

// MaskAPIKey renders a stored key as prefix + asterisks, e.g.
// "sk_op_a1b2c3d4••••••••••••".
func MaskAPIKey(prefix string) string {
	if len(prefix) > 12 {
		prefix = prefix[:12]
	}
	return prefix + strings.Repeat("•", 12)
}

// MaskCardLast4 keeps only the last four digits and hides the rest; ClearSettle
// never stores full PANs at all.
func MaskCard(last4 string) string {
	if len(last4) > 4 {
		last4 = last4[len(last4)-4:]
	}
	return "•••• •••• •••• " + last4
}
