// Package model 定义依赖图的领域模型：包版本、依赖声明与解析结果公共类型。
package model

// Dependency 是一条依赖声明：name + 区间约束（原始字符串）。
type Dependency struct {
	Name       string `json:"name"`
	Constraint string `json:"constraint"`
}

// PackageVersion 是注册表中一个具体版本及其依赖。
type PackageVersion struct {
	Version string       `json:"version"`
	Deps    []Dependency `json:"deps"`
}

// Package 是注册表中一个包的全部版本。
type Package struct {
	Name     string           `json:"name"`
	Versions []PackageVersion `json:"versions"`
}

// RegistryInput 是 JSON 接口里的内存注册表。
type RegistryInput struct {
	Packages []Package `json:"packages"`
}
