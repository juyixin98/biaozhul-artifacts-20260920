// Package governor 实现指标标签组合的基数预算治理。
//
// 核心规则：
//   - 每个指标名最多保留 Config.SeriesBudget 个不同的“标签组合”（series）；
//   - 预算用尽后，新组合不再分配内存，而是把样本累加进该指标的 overflow 桶
//     （标签固定为 {"__bucket__": "overflow"}）；
//   - 已保留的组合永不被新标签挤走（不做 LRU/淘汰，SeriesEvicted 恒为 0）；
//   - 全局样本计数守恒：Received == Accepted + Overflowed + Rejected；
//   - 标签键/值长度受限，超长值默认按 UTF-8 边界截断（可配置为拒绝）。
package governor

// OverflowLabel 是 overflow 桶使用的保留标签键，OverflowValue 是其取值。
// 普通用户标签键不允许使用 "__" 前缀（见 validateLabelKey），因此不会冲突。
const (
	OverflowLabel = "__bucket__"
	OverflowValue = "overflow"
)

// 拒绝原因常量，同时出现在 HTTP 响应的 reason 字段中。
const (
	ReasonInvalidMetricName    = "invalid_metric_name"
	ReasonMetricNameTooLong    = "metric_name_too_long"
	ReasonMetricBudgetExceeded = "metric_name_budget_exceeded"
	ReasonTooManyLabels        = "too_many_labels"
	ReasonInvalidLabelKey      = "invalid_label_key"
	ReasonLabelKeyTooLong      = "label_key_too_long"
	ReasonReservedLabelKey     = "reserved_label_key"
	ReasonLabelValueTooLong    = "label_value_too_long"
	ReasonInvalidValue         = "invalid_value"
)

// Config 控制基数预算与标签限制。零值字段会在 NewStore 中填入默认值。
type Config struct {
	// DefaultSeriesBudget 每个指标允许保留的不同标签组合数（不含 overflow 桶）。
	DefaultSeriesBudget int
	// MetricBudgets 可为个别指标覆盖预算；指标首次出现时惰性生效。
	MetricBudgets map[string]int

	// MaxMetricNames 全局允许的不同指标名数量（全局基数上限）。
	MaxMetricNames int
	// MaxMetricNameBytes 指标名最大字节数。
	MaxMetricNameBytes int

	// MaxLabelsPerSeries 单个样本允许的标签键数量。
	MaxLabelsPerSeries int
	// MaxLabelKeyBytes 标签键最大字节数（超长直接拒绝）。
	MaxLabelKeyBytes int
	// MaxLabelValueBytes 标签值最大字节数。
	MaxLabelValueBytes int
	// TruncateLabelValues 为 true 时超长值被截断；为 false 时拒绝该样本。
	TruncateLabelValues bool
}

// DefaultConfig 返回一套偏保守的默认配置。
func DefaultConfig() Config {
	return Config{
		DefaultSeriesBudget: 10000,
		MetricBudgets:       map[string]int{},
		MaxMetricNames:      512,
		MaxMetricNameBytes:  256,
		MaxLabelsPerSeries:  32,
		MaxLabelKeyBytes:    128,
		MaxLabelValueBytes:  256,
		TruncateLabelValues: true,
	}
}

func (c *Config) applyDefaults() {
	d := DefaultConfig()
	if c.DefaultSeriesBudget <= 0 {
		c.DefaultSeriesBudget = d.DefaultSeriesBudget
	}
	if c.MetricBudgets == nil {
		c.MetricBudgets = map[string]int{}
	}
	if c.MaxMetricNames <= 0 {
		c.MaxMetricNames = d.MaxMetricNames
	}
	if c.MaxMetricNameBytes <= 0 {
		c.MaxMetricNameBytes = d.MaxMetricNameBytes
	}
	if c.MaxLabelsPerSeries <= 0 {
		c.MaxLabelsPerSeries = d.MaxLabelsPerSeries
	}
	if c.MaxLabelKeyBytes <= 0 {
		c.MaxLabelKeyBytes = d.MaxLabelKeyBytes
	}
	if c.MaxLabelValueBytes <= 0 {
		c.MaxLabelValueBytes = d.MaxLabelValueBytes
	}
}

// SeriesBudget 返回指定指标实际生效的组合预算。
func (c Config) SeriesBudget(metric string) int {
	if b, ok := c.MetricBudgets[metric]; ok && b > 0 {
		return b
	}
	return c.DefaultSeriesBudget
}
