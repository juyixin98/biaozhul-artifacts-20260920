package servicetest

import "errors"

// errHookStop 通知复核作业在当前块中断（模拟进程中断/服务关闭）。
var errHookStop = errors.New("test hook: interrupt job here")
