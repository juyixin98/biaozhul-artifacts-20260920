;; 陷阱后写入示例
;;
;; 行为：
;;   1. 读取一个输入字节作为写入值
;;   2. gas::kv_put 把 "poisoned" 写入事务缓冲
;;   3. 紧接着执行 unreachable 触发陷阱
;; 预期：status = trap，事务缓冲整体丢弃，"poisoned" 永不出现在宿主 KV。
(module
  (import "gas" "input_read" (func $input_read (param i32 i32) (result i32)))
  (import "gas" "kv_put"     (func $kv_put     (param i32 i32 i32) (result i32)))

  (memory (export "memory") 1)

  (data (i32.const 0) "poisoned")

  (func (export "run") (result i32)
    (drop (call $input_read (i32.const 256) (i32.const 1)))
    ;; 写入事务缓冲（尚未发布）
    (drop (call $kv_put (i32.const 0) (i32.const 256) (i32.const 1)))
    ;; 立刻陷阱：宿主必须回滚上面的写入
    (unreachable)
    (i32.const 0)))
