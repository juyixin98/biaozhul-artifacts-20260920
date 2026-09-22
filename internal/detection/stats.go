package detection

import "math"

// MeanStddev returns the population mean and standard deviation of the samples.
func MeanStddev(samples []float64) (float64, float64) {
	if len(samples) == 0 {
		return 0, 0
	}
	var sum float64
	for _, v := range samples {
		sum += v
	}
	mean := sum / float64(len(samples))
	var sq float64
	for _, v := range samples {
		d := v - mean
		sq += d * d
	}
	return mean, math.Sqrt(sq / float64(len(samples)))
}

// ZScore returns how many standard deviations x lies from the mean. It returns
// +Inf when stddev is 0 and x > mean, -Inf when stddev is 0 and x < mean,
// and 0 when both are equal.
func ZScore(mean, stddev, x float64) float64 {
	if stddev == 0 {
		switch {
		case x > mean:
			return math.Inf(1)
		case x < mean:
			return math.Inf(-1)
		}
		return 0
	}
	return (x - mean) / stddev
}
