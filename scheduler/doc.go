// Package scheduler implements a fixed-size work-stealing task executor.
//
// The design goals are:
//
//   - A fixed pool of worker goroutines; each worker owns a local
//     double-ended task queue and may steal from other workers.
//   - Tasks may spawn child tasks and wait for them. A worker that is
//     waiting for a task keeps executing other work (including children
//     of the task it is waiting for), so a single-worker executor can
//     run arbitrarily deep spawn/wait trees without deadlock.
//   - Cancellation cooperatively cancels running task contexts and
//     eagerly cancels pending subtrees; every task runs its function at
//     most once and reaches exactly one terminal state.
//   - The clock (wall time, timers) and the event sink are interfaces,
//     so timing-dependent code is testable with a mock clock and every
//     state transition can be observed as a structured event.
package scheduler
