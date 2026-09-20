package safeopen

import "strings"

// splitRelPath 将相对路径拆成分量并拒绝任何越界形式。
// 必须在任何 path.Clean 归一化之前调用：像 "a//../x" 这类输入含空分量与
// ".."，应当直接拒绝，而不是被清洗成看似安全的路径。
func splitRelPath(relPath string) ([]string, error) {
	if relPath == "" {
		return nil, ErrPathTraversal
	}
	if strings.HasPrefix(relPath, "/") {
		return nil, ErrPathTraversal
	}
	raw := strings.Split(relPath, "/")
	out := make([]string, 0, len(raw))
	for _, c := range raw {
		switch c {
		case "", ".", "..":
			return nil, ErrPathTraversal
		}
		out = append(out, c)
	}
	return out, nil
}

// ValidateRelPath 对相对路径做安全词法校验，返回规范化后的相对路径
// （仅以单个 '/' 连接分量）。拒绝绝对路径、空分量、"."、".."。
func ValidateRelPath(relPath string) (string, error) {
	parts, err := splitRelPath(relPath)
	if err != nil {
		return "", err
	}
	return strings.Join(parts, "/"), nil
}
