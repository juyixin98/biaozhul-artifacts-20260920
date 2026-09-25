# taintflow 小语言（TFL）语言参考

本文件是 taintflow 自定义小语言的权威定义。分析器只接受符合本语法与语义的
程序。文件后缀约定为 `.tfl`。

## 1. 词法

### 1.1 记号与空白

- 空白（空格、制表符、回车、换行）仅用于分隔记号。
- 行注释：`//` 到行尾。
- 块注释：`/* ... */`，**可以嵌套**（`/* a /* b */ c */` 合法）。
- 标识符：`[A-Za-z_][A-Za-z0-9_]*`。
- 整数：`[0-9]+`（仅非负整数字面量；负数由一元 `-` 表达）。
- 字符串：`"..."` 或 `'...'`，不允许跨行；支持转义
  `\n \t \r \" \' \\`（其它 `\x` 退化为 `x`）。
- 布尔字面量：关键字 `true`、`false`。

### 1.2 关键字

```
fn  if  else  while  return  true  false  and  or  not
```

`and/or/not` 分别等价于运算符 `&&`/`||`/`!`。

### 1.3 标点与运算符

```
( ) { } , ;
=  ==  !=  <  >  <=  >=
+  -  *  /  %
&&  ||  !
```

多字符运算符（`== != <= >= && ||`）优先于其前缀匹配。

## 2. 语法（EBNF）

```ebnf
program        = function_decl+ ;

function_decl  = 'fn' IDENT '(' [ param_list ] ')' block ;
param_list     = IDENT { ',' IDENT } ;
block          = '{' { statement } '}' ;

statement      = ';'
               | 'return' [ expression ] ';'
               | 'if' '(' expression ')' block
                 [ 'else' ( block | 'if' '(' expression ')' block
                            [ 'else' ( block | if_statement ) ] ) ]
               | 'while' '(' expression ')' block
               | expression ';'
               | IDENT '=' expression ';'
               ;

expression     = expr_or ;
expr_or        = expr_and { ('||' | 'or') expr_and } ;
expr_and       = expr_equality { ('&&' | 'and') expr_equality } ;
expr_equality  = expr_relational { ('==' | '!=') expr_relational } ;
expr_relational= expr_additive { ('<' | '>' | '<=' | '>=') expr_additive } ;
expr_additive  = expr_multiplicative { ('+' | '-') expr_multiplicative } ;
expr_multiplicative = expr_unary { ('*' | '/' | '%') expr_unary } ;
expr_unary     = ('!' | 'not' | '-') expr_unary
               | expr_call_or_primary ;
expr_call_or_primary
               = IDENT '(' [ arg_list ] ')'    (* 仅直接调用 *)
               | primary ;
arg_list       = expression { ',' expression } ;
primary        = INTEGER | STRING | 'true' | 'false'
               | IDENT
               | '(' expression ')' ;
```

### 优先级（从低到高）与结合性

1. `||` / `or`
2. `&&` / `and`
3. `==` `!=`
4. `<` `>` `<=` `>=`
5. `+` `-`
6. `*` `/` `%`
7. 一元 `!`/`not`/`-`
8. 调用、基本式

所有二元运算符**左结合**。赋值不是表达式，没有右值链/复合赋值/自增。

### dangling-else

`else` 与词法上最近的、尚未匹配 else 的 `if` 结合。

## 3. 语义（与分析相关的部分）

### 3.1 类型与值

值有四类：整数、字符串、布尔、空值。语言是弱类型的（不做静态类型检查）；
运算符按通用算术/比较/逻辑规则理解。分析器**不计算具体值**，只关心污点，
因此除“是否为常量字面量”外不依赖运行时取值。

### 3.2 作用域与变量

- 函数是唯一作用域；形参在整个函数体内可见。
- 变量首次赋值即定义；无 `var/let` 声明。
- 无块级作用域：分支/循环内赋值的变量在外部同样可见。
- 读取“在任何路径上都未赋值”的变量时，IR 构建产生 `uninitialized_read`
  警告；该变量按**干净空值**处理（不会凭空成为污点）。

### 3.3 函数与调用

- 只有顶层函数、只有直接调用 `f(args)`；无函数值、方法、闭包、间接调用。
- 实参个数必须等于形参个数，否则分析报错。
- 函数可递归、相互递归。
- 无 `return` 走到函数末尾等价于返回空值。
- 调用未定义函数：默认按保守污点传播处理（FP-1），可配置为硬错误。

### 3.4 内置：源 / 清洗器 / 汇

| 内置 | 签名 | 污点语义 |
|---|---|---|
| source（可改名） | `() -> T` | 返回一个全新污点值（**无参**） |
| clean / 清洗器（可改名） | `(T) -> U` | 返回干净值；入参的污点不传播到结果 |
| sink / 汇（可改名） | `(T) -> void` | 实参带污点时产生一条 source→sink 告警 |

`clean` **不修改**实参变量本身：

```
a = source();
b = clean(a);   // b 干净
sink(a);        // 仍告警：a 依旧带污点
sink(b);        // 不告警
```

内置名字都可配置为任意标识符；用户函数不能与内置同名。

### 3.5 控制流

- `if`：条件为真执行 then，否则执行 else（若有）；分析器两支都计入。
- `while (c) BODY`：标准当型循环；分析器把 0 次、1 次、…… 的效果在不动点中
  合并（∪），因此循环体内的清洗对“0 次迭代路径”不生效（FP-2 的循环情形）。

## 4. 一个完整例子

```
// 递归把污点搬运到 sink
fn walk(n, acc) {
    if (n == 0) {
        return acc;
    } else {
        return walk(n - 1, acc);
    }
}

fn main() {
    t = source();
    r = walk(10, t);
    sink(r);
}
```

## 5. 明确不支持的特性

数组、对象/结构体、指针、字段访问、索引、字符串拼接之外的字符串操作、
函数值/高阶函数、方法调用、闭包、异常、并发、赋值形式的复合表达式、
自增自减、`break/continue`、全局变量、import/模块。
