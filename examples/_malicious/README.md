# 恶意/畸形归档样例

每个文件都应被解包器以 400 BAD_PACKAGE 拒绝：
- zipslip.zip：`../` 路径穿越
- absolute.zip：绝对路径
- backslash.zip：反斜杠路径歧义
- symlink.tar：符号链接
- hardlink.tar：硬链接
- duplicate.zip：规范化后条目冲突
- nul.zip：NUL 控制字符
