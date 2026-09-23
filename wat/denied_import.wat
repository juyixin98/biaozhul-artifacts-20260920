;; 样例 6：非白名单导入。
;;
;; 该模块尝试导入 env.socket_connect（网络访问），不在宿主白名单内。
;; 链接（实例化）阶段必须失败，模块代码不会执行，也不产生任何状态。
(module
  (import "env" "socket_connect" (func $socket_connect (param i32 i32)))

  (memory (export "memory") 1)

  (func (export "alloc") (param $size i32) (result i32)
    (i32.const 64))

  (func (export "run") (param $in_ptr i32) (param $in_len i32) (result i64)
    (call $socket_connect (i32.const 0) (i32.const 0))
    (i64.const 0))
)
