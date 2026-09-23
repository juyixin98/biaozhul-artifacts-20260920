;; 样例 7：宿主函数指针越界。
;;
;; 键指针合法（指向 "x"），但值输出指针指向线性内存末端之外。
;; 宿主在写入前做边界校验，必须以越界陷阱终止，且不提交任何状态。
(module
  (import "env" "kv_get" (func $kv_get (param i32 i32 i32 i32) (result i32)))

  (memory (export "memory") 1)
  (data (i32.const 0) "x")

  (func (export "alloc") (param $size i32) (result i32)
    (i32.const 64))

  (func (export "run") (param $in_ptr i32) (param $in_len i32) (result i64)
    (drop (call $kv_get
      (i32.const 0) (i32.const 1)
      ;; memory.size * 64KiB = 末端边界，从这里写一定越界。
      (i32.shl (memory.size) (i32.const 16))
      (i32.const 4)))
    (i64.const 0))
)
