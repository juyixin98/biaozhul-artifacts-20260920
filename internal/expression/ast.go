package expression

// Node 是表达式 AST 的通用接口。
type Node interface {
	nodeMarker()
}

// LicenseNode 表示单个许可证原子，例如 MIT、LicenseRef-Internal 或
// DocumentRef-x:LicenseRef-y。
type LicenseNode struct {
	License string
}

// WithNode 表示 "许可证 WITH 例外" 组合，例如 LGPL-2.1-only WITH Classpath-exception-2.0。
type WithNode struct {
	License   *LicenseNode
	Exception string
}

// AndNode 表示逻辑与（AND）。
type AndNode struct {
	Left  Node
	Right Node
}

// OrNode 表示逻辑或（OR）。
type OrNode struct {
	Left  Node
	Right Node
}

func (*LicenseNode) nodeMarker() {}
func (*WithNode) nodeMarker()    {}
func (*AndNode) nodeMarker()     {}
func (*OrNode) nodeMarker()      {}
