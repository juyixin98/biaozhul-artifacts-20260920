;; 宿主越界访问示例
;;
;; 行为：先往事务缓冲写一条，然后调用 gas::output_write 时传入
;;       完全超出线性内存的 (ptr=0x7FFFFFFF, len=16)。
;; 预期：宿主边界检查捕获 -> trap（out-of-bounds host memory access），
;;       缓冲写入回滚，不提交任何状态。
(module
  (import "gas" "output_write" (func $output_write (param i32 i32) (result i32)))
  (import "gas" "kv_put"       (func $kv_put       (param i32 i32 i32) (result i32)))

  (memory (export "memory") 1)
  (data (i32.const 0) "ghost")

  (func (export "run") (result i32)
    ;; 先写缓冲
    (drop (call $kv_put (i32.const 0) (i32.const 0) (i32.const 5)))
    ;; 越界读取：宿主必须拒绝并陷阱
    (drop (call $output_write (i32.const 0x7FFFFFFF) (i32.const 16)))
    (i32.const 0)))
