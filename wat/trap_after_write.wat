;; 样例 3：陷阱后写入。
;;
;; 先 kv_put("poison", "2") 向事务缓冲写入，随后执行 unreachable 触发陷阱。
;; 事务必须整体丢弃：提交态中不应出现 "poison"。
(module
  (import "env" "kv_put" (func $kv_put (param i32 i32 i32 i32)))

  (memory (export "memory") 1)
  (data (i32.const 0) "poison\00\00")
  (data (i32.const 32) "2")

  (func (export "alloc") (param $size i32) (result i32)
    (i32.const 64))

  (func (export "run") (param $in_ptr i32) (param $in_len i32) (result i64)
    (call $kv_put (i32.const 0) (i32.const 6) (i32.const 32) (i32.const 1))
    (unreachable)
    (i64.const 0))
)
