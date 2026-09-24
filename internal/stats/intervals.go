// Package stats computes the confidence intervals that every rollout decision
// must cite as evidence. All calculations are real — nothing is hard-coded.
package stats

import (
	"errors"
	"math"
)

// confidence95 is the confidence level used by every interval in this project.
const confidence95 = 0.95

// ZScore95 returns the standard-normal quantile for the 95% two-sided interval.
func ZScore95() float64 {
	// p = 0.975 upper-tail quantile of N(0,1), computed (not the textbook
	// constant) so the same inverse CDF serves any confidence level later.
	return NormalInvCDF(1 - (1-confidence95)/2)
}

// NormalInvCDF is the inverse cumulative distribution function of the standard
// normal distribution (quantile function), Peter J. Acklam's rational
// approximation; relative error < 1.15e-9.
func NormalInvCDF(p float64) float64 {
	if p <= 0 {
		return math.Inf(-1)
	}
	if p >= 1 {
		return math.Inf(1)
	}

	const (
		a1 = -3.969683028665376e+01
		a2 = 2.209460984245205e+02
		a3 = -2.759285104469687e+02
		a4 = 1.383577518672690e+02
		a5 = -3.066479806614716e+01
		a6 = 2.506628277459239e+00

		b1 = -5.447609879822406e+01
		b2 = 1.615858368580409e+02
		b3 = -1.556989798598866e+02
		b4 = 6.680131188771972e+01
		b5 = -1.328068155288572e+01

		c1 = -7.784894002430293e-03
		c2 = -3.223964580411365e-01
		c3 = -2.400758277161838e+00
		c4 = -2.549732539343734e+00
		c5 = 4.374664141464968e+00
		c6 = 2.938163982698783e+00

		d1 = 7.784695709041462e-03
		d2 = 3.224671290700398e-01
		d3 = 2.445134137142996e+00
		d4 = 3.754408661907416e+00

		pLow  = 0.02425
		pHigh = 1 - pLow
	)

	switch {
	case p < pLow:
		q := math.Sqrt(-2 * math.Log(p))
		return (((((c1*q+c2)*q+c3)*q+c4)*q+c5)*q + c6) /
			((((d1*q+d2)*q+d3)*q+d4)*q + 1)
	case p <= pHigh:
		q := p - 0.5
		r := q * q
		return (((((a1*r+a2)*r+a3)*r+a4)*r+a5)*r + a6) * q /
			(((((b1*r+b2)*r+b3)*r+b4)*r+b5)*r + 1)
	default:
		q := math.Sqrt(-2 * math.Log(1-p))
		return -(((((c1*q+c2)*q+c3)*q+c4)*q+c5)*q + c6) /
			((((d1*q+d2)*q+d3)*q+d4)*q + 1)
	}
}

// WilsonInterval is the Wilson score interval for a Bernoulli proportion —
// the correct interval for error rates near 0 (the Wald interval collapses to
// a negative or zero bound there). lo/hi are the two-sided 95% bounds in [0,1].
// n == 0 yields no interval and the caller treats it as missing data.
func WilsonInterval(errCount, n int64) (lo, hi float64, err error) {
	if n <= 0 {
		return 0, 0, errors.New("wilson: sample size must be positive")
	}
	if errCount < 0 || errCount > n {
		return 0, 0, errors.New("wilson: errors must be between 0 and n")
	}
	z := ZScore95()
	p := float64(errCount) / float64(n)
	z2 := z * z
	denom := 1 + z2/float64(n)
	center := (p + z2/(2*float64(n))) / denom
	half := (z * math.Sqrt(p*(1-p)/float64(n)+z2/(4*float64(n*n)))) / denom
	return center - half, center + half, nil
}

// MeanTInterval is the Student-t confidence interval for the mean of the
// latency samples. n must be >= 2; n == 1 cannot yield a sample variance.
type MeanInterval struct {
	Mean    float64
	StdDev  float64
	Lower   float64
	Upper   float64
	N       int
	DegFree int
}

func MeanTInterval(samples []float64) (MeanInterval, error) {
	var out MeanInterval
	n := len(samples)
	if n < 2 {
		return out, errors.New("t-interval: need at least 2 samples")
	}
	sum := 0.0
	for _, v := range samples {
		sum += v
	}
	mean := sum / float64(n)
	sq := 0.0
	for _, v := range samples {
		d := v - mean
		sq += d * d
	}
	sd := math.Sqrt(sq / float64(n-1))
	// Two-sided 95% interval -> upper-tail probability (1-0.95)/2 = 0.025.
	tcrit := StudentTQuantile(1-(1-confidence95)/2, float64(n-1))
	se := sd / math.Sqrt(float64(n))
	return MeanInterval{
		Mean:    mean,
		StdDev:  sd,
		Lower:   mean - tcrit*se,
		Upper:   mean + tcrit*se,
		N:       n,
		DegFree: n - 1,
	}, nil
}

// StudentTQuantile returns t_{p,nu}, the quantile p of Student's t with nu
// degrees of freedom. It is computed by inverting the regularized incomplete
// beta function (the CDF of t) with bisection.
func StudentTQuantile(p, nu float64) float64 {
	if p <= 0 {
		return math.Inf(-1)
	}
	if p >= 1 {
		return math.Inf(1)
	}
	if nu <= 0 {
		return math.NaN()
	}
	if math.Abs(p-0.5) < 1e-15 {
		return 0
	}
	neg := p < 0.5
	if neg {
		p = 1 - p
	}
	// On the positive half: F(t) = 1 - 0.5*I_x(nu/2, 1/2),
	// x = nu/(nu+t^2). Solve F(t) = p.
	target := 2 * (1 - p) // = I_x
	// I_x(a,b) monotonically decreases in t, so bisect t in [0, big].
	lo, hi := 0.0, 1.0
	a, b := nu/2, 0.5
	for ibeta(nu/(nu+hi*hi), a, b) > target {
		hi *= 2
		if hi > 1e6 {
			break
		}
	}
	for i := 0; i < 100; i++ {
		mid := (lo + hi) / 2
		x := nu / (nu + mid*mid)
		if ibeta(x, a, b) > target {
			lo = mid
		} else {
			hi = mid
		}
	}
	t := (lo + hi) / 2
	if neg {
		return -t
	}
	return t
}

// ibeta is the regularized incomplete beta function I_x(a,b), evaluated with
// continued fractions (Numerical Recipes, betai).
func ibeta(x, a, b float64) float64 {
	if x <= 0 {
		return 0
	}
	if x >= 1 {
		return 1
	}
	lbeta := lgamma(a+b) - lgamma(a) - lgamma(b)
	front := math.Exp(lbeta + a*math.Log(x) + b*math.Log(1-x))
	// Lentz's method for the continued fraction, choosing the convergence
	// direction that keeps |x| below (a+1)/(a+b+2).
	if x < (a+1)/(a+b+2) {
		return front * betacf(a, b, x) / a
	}
	return 1 - front*betacf(b, a, 1-x)/b
}

func betacf(a, b, x float64) float64 {
	const maxIter = 200
	const eps = 3e-12
	const fpMin = 1e-30

	qab := a + b
	qap := a + 1
	qam := a - 1
	c := 1.0
	d := 1 - qab*x/qap
	if math.Abs(d) < fpMin {
		d = fpMin
	}
	d = 1 / d
	h := d
	for m := 1; m <= maxIter; m++ {
		mf := float64(m)
		m2 := 2 * mf

		aa := mf * (b - mf) * x / ((qam + m2) * (a + m2))
		d = 1 + aa*d
		if math.Abs(d) < fpMin {
			d = fpMin
		}
		c = 1 + aa/c
		if math.Abs(c) < fpMin {
			c = fpMin
		}
		d = 1 / d
		h *= d * c

		aa = -(a + mf) * (qab + mf) * x / ((a + m2) * (qap + m2))
		d = 1 + aa*d
		if math.Abs(d) < fpMin {
			d = fpMin
		}
		c = 1 + aa/c
		if math.Abs(c) < fpMin {
			c = fpMin
		}
		d = 1 / d
		delta := d * c
		h *= delta
		if math.Abs(delta-1) < eps {
			break
		}
	}
	return h
}

// lgamma returns log Gamma(x) using the Lanczos approximation with g=9, n=7.
func lgamma(x float64) float64 {
	coeff := []float64{
		0.99999999999980993,
		676.5203681218851,
		-1259.1392167224028,
		771.32342877765313,
		-176.61502916214059,
		12.507343278686905,
		-0.13857109526572012,
		9.9843695780195716e-6,
		1.5056327351493116e-7,
	}
	if x < 0.5 {
		return math.Log(math.Pi/math.Sin(math.Pi*x)) - lgamma(1-x)
	}
	x -= 1
	ag := coeff[0]
	for i := 1; i < len(coeff); i++ {
		ag += coeff[i] / (x + float64(i))
	}
	t := x + 7.5 // Lanczos g=7: there are 8 coefficients beyond c0
	return 0.5*math.Log(2*math.Pi) + (x+0.5)*math.Log(t) - t + math.Log(ag)
}
