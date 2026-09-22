package detection_test

import (
	"encoding/json"
	"strconv"
	"time"

	"anomalywatch/internal/models"
)

func itoa(i int) string { return strconv.Itoa(i) }

func jsonMarshal(m models.JSONMap) ([]byte, error) { return json.Marshal(m) }

func countInEvidence(m models.JSONMap, key string) int {
	v, ok := m[key]
	if !ok {
		return 0
	}
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case json.Number:
		i, _ := n.Int64()
		return int(i)
	}
	return 0
}

func mustLoc(name string) *time.Location {
	loc, err := time.LoadLocation(name)
	if err != nil {
		panic(err)
	}
	return loc
}
