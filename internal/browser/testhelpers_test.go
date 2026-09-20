package browser_test

import "sitevitals/internal/models"

func hasViolation(vs []models.Violation, kind string) bool {
	for _, v := range vs {
		if v.Kind == kind {
			return true
		}
	}
	return false
}
