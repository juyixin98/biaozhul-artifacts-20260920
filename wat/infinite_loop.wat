;; 样例 2：无限循环。
;;
;; 先 kv_put("poison", "1") 向事务缓冲写入，随后进入死循环，
;; 直到燃料耗尽被宿主终止。由于执行失败，"poison" 不得发布。
(module
  (import "env" "kv_put" (func $kv_put (param i32 i32 i32 i32)))

  (memory (export "memory") 1)
  (data (i32.const 0) "poison\00\00")
  (data (i32.const 32) "1")

  (func (export "alloc") (param $size i32) (result i32)
    (i32.const 64))

  (func (export "run") (param $in_ptr i32) (param $in_len i32) (result i64)
    ;; 事务缓冲写入（失败后必须回滚）。
    (call $kv_put (i32.const 0) (i32.const 6) (i32.const 32) (i32.const 1))
    ;; 死循环：仅靠燃料耗尽终止。
    (loop $forever
      (br $forever))
    (i64.const 0))
)
