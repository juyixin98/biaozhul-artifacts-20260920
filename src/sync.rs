//! Small dependency-free concurrency primitives built on `std`:
//! a bounded MPSC channel, a counting semaphore and a cancellation token.

use std::collections::VecDeque;
use std::sync::{Arc, Condvar, Mutex};

// ---------------------------------------------------------------------------
// Bounded MPSC channel
// ---------------------------------------------------------------------------

struct ChannelState<T> {
    queue: VecDeque<T>,
    capacity: usize,
    senders: usize,
    disconnected: bool,
}

struct Shared<T> {
    state: Mutex<ChannelState<T>>,
    /// Woken on "queue not full" (blocked senders) and on shutdown.
    on_space: Condvar,
    /// Woken on "queue not empty" (receiver) and on shutdown.
    on_item: Condvar,
    shutdown: Mutex<bool>,
    /// Broadcast condvar used to release all blocked waits on shutdown.
    on_shutdown: Condvar,
}

/// Outcome of a bounded receive with a deadline.
#[derive(Debug, PartialEq, Eq)]
pub enum RecvTimeout<T> {
    /// An item was received.
    Item(T),
    /// All senders were dropped and the queue had drained.
    Closed,
    /// The deadline elapsed before an item or closure arrived.
    TimedOut,
}

pub fn bounded_channel<T>(capacity: usize) -> (BoundedSender<T>, BoundedReceiver<T>) {
    assert!(capacity >= 1, "channel capacity must be >= 1");
    let shared = Arc::new(Shared {
        state: Mutex::new(ChannelState {
            queue: VecDeque::with_capacity(capacity.min(64)),
            capacity,
            senders: 1,
            disconnected: false,
        }),
        on_space: Condvar::new(),
        on_item: Condvar::new(),
        shutdown: Mutex::new(false),
        on_shutdown: Condvar::new(),
    });
    (
        BoundedSender {
            shared: shared.clone(),
        },
        BoundedReceiver { shared },
    )
}

#[derive(Debug)]
pub struct SendError<T>(pub T);

pub struct BoundedSender<T> {
    shared: Arc<Shared<T>>,
}

impl<T> std::fmt::Debug for BoundedSender<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BoundedSender").finish_non_exhaustive()
    }
}

pub struct BoundedReceiver<T> {
    shared: Arc<Shared<T>>,
}

impl<T> Clone for BoundedSender<T> {
    /// Every clone is one more live sender; the receiver waits until *all*
    /// senders are dropped before reporting end-of-stream.
    fn clone(&self) -> Self {
        {
            let mut st = self.shared.state.lock().unwrap();
            st.senders += 1;
        }
        BoundedSender {
            shared: self.shared.clone(),
        }
    }
}

impl<T> BoundedSender<T> {
    /// Blocking send with backpressure. Returns an error after shutdown.
    pub fn send(&self, value: T) -> Result<(), SendError<T>> {
        let mut st = self.shared.state.lock().unwrap();
        loop {
            if *self.shared.shutdown.lock().unwrap() || st.disconnected {
                return Err(SendError(value));
            }
            if st.queue.len() < st.capacity {
                st.queue.push_back(value);
                drop(st);
                self.shared.on_item.notify_one();
                return Ok(());
            }
            st = self.shared.on_space.wait(st).unwrap();
        }
    }

    /// Non-blocking send: [`None`] on success, `Some(value)` if the queue is
    /// full or the channel is shut down.
    pub fn try_send(&self, value: T) -> Result<(), T> {
        let mut st = self.shared.state.lock().unwrap();
        if *self.shared.shutdown.lock().unwrap() || st.disconnected {
            return Err(value);
        }
        if st.queue.len() >= st.capacity {
            return Err(value);
        }
        st.queue.push_back(value);
        drop(st);
        self.shared.on_item.notify_one();
        Ok(())
    }

    pub fn shutdown(&self) {
        *self.shared.shutdown.lock().unwrap() = true;
        self.shared.on_shutdown.notify_all();
        self.shared.on_item.notify_all();
        self.shared.on_space.notify_all();
    }

    pub fn is_shutdown(&self) -> bool {
        *self.shared.shutdown.lock().unwrap()
    }
}

impl<T> Drop for BoundedSender<T> {
    fn drop(&mut self) {
        let mut st = self.shared.state.lock().unwrap();
        st.senders -= 1;
        if st.senders == 0 {
            drop(st);
            self.shared.on_item.notify_all();
        }
    }
}

impl<T> BoundedReceiver<T> {
    /// Blocking receive. `Ok(None)` means all senders dropped / shutdown and
    /// the queue has drained.
    pub fn recv(&self) -> Option<T> {
        let mut st = self.shared.state.lock().unwrap();
        loop {
            if let Some(v) = st.queue.pop_front() {
                drop(st);
                self.shared.on_space.notify_one();
                return Some(v);
            }
            if st.senders == 0 {
                return None;
            }
            st = self.shared.on_item.wait(st).unwrap();
        }
    }

    /// Wait for an item with a timeout.
    pub fn recv_timeout(&self, timeout: std::time::Duration) -> RecvTimeout<T> {
        let start = std::time::Instant::now();
        let mut st = self.shared.state.lock().unwrap();
        loop {
            if let Some(v) = st.queue.pop_front() {
                drop(st);
                self.shared.on_space.notify_one();
                return RecvTimeout::Item(v);
            }
            if st.senders == 0 {
                return RecvTimeout::Closed;
            }
            let elapsed = start.elapsed();
            if elapsed >= timeout {
                return RecvTimeout::TimedOut;
            }
            let (guard, result) = self
                .shared
                .on_item
                .wait_timeout(st, timeout - elapsed)
                .unwrap();
            st = guard;
            if result.timed_out() {
                if let Some(v) = st.queue.pop_front() {
                    drop(st);
                    self.shared.on_space.notify_one();
                    return RecvTimeout::Item(v);
                }
                return RecvTimeout::TimedOut;
            }
        }
    }

    pub fn shutdown(&self) {
        *self.shared.shutdown.lock().unwrap() = true;
        self.shared.on_shutdown.notify_all();
        self.shared.on_item.notify_all();
        self.shared.on_space.notify_all();
    }
}

// ---------------------------------------------------------------------------
// Counting semaphore (bounds in-flight work)
// ---------------------------------------------------------------------------

struct SemState {
    permits: usize,
}

/// Blocking counting semaphore.
pub struct Semaphore {
    state: Mutex<SemState>,
    cv: Condvar,
}

impl Semaphore {
    pub fn new(permits: usize) -> Semaphore {
        Semaphore {
            state: Mutex::new(SemState { permits }),
            cv: Condvar::new(),
        }
    }

    pub fn acquire(self: &Arc<Self>) -> SemaphorePermit {
        let mut st = self.state.lock().unwrap();
        while st.permits == 0 {
            st = self.cv.wait(st).unwrap();
        }
        st.permits -= 1;
        SemaphorePermit {
            sem: Arc::clone(self),
        }
    }

    /// Returns a permit immediately, or `None` if none are available.
    pub fn try_acquire(self: &Arc<Self>) -> Option<SemaphorePermit> {
        let mut st = self.state.lock().unwrap();
        if st.permits == 0 {
            return None;
        }
        st.permits -= 1;
        Some(SemaphorePermit {
            sem: Arc::clone(self),
        })
    }

    pub fn available(&self) -> usize {
        self.state.lock().unwrap().permits
    }

    fn release(&self) {
        let mut st = self.state.lock().unwrap();
        st.permits += 1;
        self.cv.notify_one();
    }
}

pub struct SemaphorePermit {
    sem: Arc<Semaphore>,
}

impl Drop for SemaphorePermit {
    fn drop(&mut self) {
        self.sem.release();
    }
}

// ---------------------------------------------------------------------------
// Cancellation token: a one-shot flag many waiters can observe
// ---------------------------------------------------------------------------

struct TokenState {
    cancelled: bool,
}

/// Cloneable cancellation flag. [`Token::cancel`] wakes every waiter.
#[derive(Clone)]
pub struct Token {
    inner: Arc<TokenInner>,
}

struct TokenInner {
    state: Mutex<TokenState>,
    cv: Condvar,
}

impl Token {
    pub fn new() -> Token {
        Token {
            inner: Arc::new(TokenInner {
                state: Mutex::new(TokenState { cancelled: false }),
                cv: Condvar::new(),
            }),
        }
    }

    pub fn cancel(&self) {
        let mut st = self.inner.state.lock().unwrap();
        st.cancelled = true;
        self.inner.cv.notify_all();
    }

    pub fn is_cancelled(&self) -> bool {
        self.inner.state.lock().unwrap().cancelled
    }

    /// Block until cancelled (used by slow workers that want to wake up early
    /// instead of sleeping through their whole delay).
    pub fn wait_cancelled(&self) {
        let mut st = self.inner.state.lock().unwrap();
        while !st.cancelled {
            st = self.inner.cv.wait(st).unwrap();
        }
    }

    /// Sleep at most `dur`, waking early on cancellation. Returns true if the
    /// token was cancelled during the sleep.
    pub fn sleep_or_cancelled(&self, dur: std::time::Duration) -> bool {
        let start = std::time::Instant::now();
        let mut st = self.inner.state.lock().unwrap();
        while !st.cancelled {
            let elapsed = start.elapsed();
            if elapsed >= dur {
                return false;
            }
            let (guard, _) = self.inner.cv.wait_timeout(st, dur - elapsed).unwrap();
            st = guard;
        }
        true
    }
}

impl Default for Token {
    fn default() -> Self {
        Token::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn bounded_channel_blocks_when_full() {
        let (tx, rx) = bounded_channel::<u32>(2);
        tx.send(1).unwrap();
        tx.send(2).unwrap();
        assert_eq!(tx.try_send(3), Err(3));
        assert_eq!(rx.recv(), Some(1));
        tx.send(3).unwrap();
        assert_eq!(rx.recv(), Some(2));
        assert_eq!(rx.recv(), Some(3));
    }

    #[test]
    fn bounded_channel_recv_timeout() {
        let (tx, rx) = bounded_channel::<u32>(1);
        assert_eq!(
            rx.recv_timeout(Duration::from_millis(20)),
            RecvTimeout::TimedOut
        );
        tx.send(7).unwrap();
        assert_eq!(
            rx.recv_timeout(Duration::from_millis(20)),
            RecvTimeout::Item(7)
        );
    }

    #[test]
    fn sender_drop_closes_receiver() {
        let (tx, rx) = bounded_channel::<u32>(1);
        tx.send(1).unwrap();
        drop(tx);
        assert_eq!(rx.recv(), Some(1));
        assert_eq!(rx.recv(), None);
    }

    #[test]
    fn semaphore_bounds_concurrency() {
        let sem = Arc::new(Semaphore::new(2));
        let p1 = sem.acquire();
        let p2 = sem.acquire();
        assert!(sem.try_acquire().is_none());
        drop(p1);
        let p3 = sem.try_acquire().expect("permit freed");
        assert!(sem.try_acquire().is_none());
        drop(p2);
        drop(p3);
        assert!(sem.try_acquire().is_some());
    }

    #[test]
    fn token_wakes_sleep_early() {
        let token = Token::new();
        let t2 = token.clone();
        let h = std::thread::spawn(move || {
            // Should wake almost immediately, not after 30 seconds.
            assert!(t2.sleep_or_cancelled(Duration::from_secs(30)));
        });
        std::thread::sleep(Duration::from_millis(10));
        token.cancel();
        h.join().unwrap();
    }

    #[test]
    fn token_sleep_completes_when_uncancelled() {
        let token = Token::new();
        assert!(!token.sleep_or_cancelled(Duration::from_millis(10)));
    }
}
