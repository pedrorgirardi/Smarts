# Thread Safety Recommendations for Smarts

This document provides educational recommendations for improving thread safety in the Smarts LSP client. Each issue includes an explanation of **why** it's a problem and **how** to fix it.

## Executive Summary

The codebase has several thread safety issues stemming from:
1. **Shared mutable state** accessed from multiple threads without synchronization
2. **UI operations** performed from background threads
3. **Race conditions** between checking state and acting on it

## Threading Architecture Overview

### Current Threads
- **Main Thread**: Sublime Text's UI thread (handles commands, event listeners)
- **Reader Thread**: Reads LSP messages from server stdout (smarts_client.py:528)
- **Writer Thread**: Writes LSP messages to server stdin (smarts_client.py:571)
- **Handler Thread**: Processes received messages and invokes callbacks (smarts_client.py:597)
- **Timer Threads**: Used for debouncing highlights (smarts.py:1865)

---

## Critical Issues

### 1. Global `_SMARTS` List - Race Condition

**Location**: `smarts.py:149`

**Problem**:
```python
_SMARTS: List[PgSmart] = []
```

This global list is accessed and modified from multiple threads:
- Modified: `remove_smarts()` (line 201), `PgSmartsInitializeCommand.run()` (line 1162)
- Read: `find_smart()` (line 205), `window_smarts()` (line 224), etc.

**Why it's dangerous**:
Python lists are **not thread-safe** for modification operations. When two threads modify a list simultaneously:
- List corruption can occur (broken internal pointers)
- Items can be lost or duplicated
- You can get `IndexError` even when checking length first (TOCTOU - Time Of Check, Time Of Use)

**Example race condition**:
```python
# Thread 1: Iterating
for smart in _SMARTS:  # Reading length and accessing elements
    # ...

# Thread 2: Simultaneously
_SMARTS = [smart for smart in _SMARTS if ...]  # Replacing entire list
```

**Solution - Add a Lock**:
```python
import threading

# At module level
_SMARTS: List[PgSmart] = []
_SMARTS_LOCK = threading.Lock()

def remove_smarts(uuids: Set[str]):
    global _SMARTS
    with _SMARTS_LOCK:
        _SMARTS = [smart for smart in _SMARTS if smart["uuid"] not in uuids]

def find_smart(uuid: str) -> Optional[PgSmart]:
    with _SMARTS_LOCK:
        for smart in _SMARTS:
            if smart["uuid"] == uuid:
                return smart
    return None

def window_smarts(window: sublime.Window) -> List[PgSmart]:
    with _SMARTS_LOCK:
        # Return a copy to avoid holding lock during iteration
        return [smart for smart in _SMARTS if smart["window"] == window.id()]
```

**Key Learning**:
- Always protect shared mutable state with locks
- When returning collections, consider returning **copies** so callers don't need to hold the lock
- Keep lock-holding time **minimal** - don't do I/O or expensive operations inside locks

---

### 2. Request Callback Dictionary - Concurrent Modification

**Location**: `smarts_client.py:446-448`

**Problem**:
```python
self._request_callback: Dict[Union[int, str], Callable] = {}
```

Accessed from multiple threads:
- **Writer thread context** (via `_put()`): Adds entries (line 681)
- **Handler thread**: Reads and deletes entries (lines 611, 617)

**Why it's dangerous**:
Python dicts have some thread-safety for certain operations in CPython (due to GIL), but:
- **Not guaranteed** in other Python implementations (PyPy, Jython)
- **Dictionary resizing** during growth can cause issues
- **Iteration during modification** can raise `RuntimeError: dictionary changed size during iteration`

**Example failure scenario**:
```python
# Handler thread
for request_id in self._request_callback:  # Iterating
    # ...

# Put thread (via different request)
self._request_callback[new_id] = callback  # Modifying - can trigger resize
# Result: RuntimeError or corrupt data
```

**Solution - Add a Lock**:
```python
class LanguageServerClient:
    def __init__(self, ...):
        # ... existing code ...
        self._request_callback: Dict[Union[int, str], Callable] = {}
        self._request_callback_lock = threading.Lock()

    def _put(self, message, callback=None):
        # ... existing checks ...

        self._send_queue.put(message)

        if message_id := message.get("id"):
            if callback:
                with self._request_callback_lock:
                    self._request_callback[message_id] = callback

    def _start_handler(self):
        # ... existing code ...

        while (message := self._receive_queue.get()) is not None:
            # ... existing code ...

            if request_id := message.get("id"):
                # Get and remove callback atomically
                with self._request_callback_lock:
                    callback = self._request_callback.pop(request_id, None)

                if callback:
                    try:
                        callback(cast(LSPResponseMessage, message))
                    except Exception:
                        self._logger.exception(f"{self._name} - Request callback error")
```

**Key Learning**:
- Use `dict.pop(key, default)` instead of `get()` + `del` for atomic remove
- Always protect dictionaries that are modified from multiple threads
- Consider using `threading.RLock()` (reentrant lock) if same thread needs to acquire lock multiple times

---

### 3. Open Documents Set - Race Condition

**Location**: `smarts_client.py:449`

**Problem**:
```python
self._open_documents = set()
```

Modified in:
- `textDocument_didOpen()` (line 857): `self._open_documents.add(textDocument_uri)`
- `textDocument_didClose()` (line 885): `self._open_documents.remove(textDocument_uri)`
- `textDocument_didChange()` (line 899): checks `if uri in self._open_documents`

These can be called from Sublime's event listeners from the main thread.

**Why it's dangerous**:
- Set modifications are **not atomic**
- Check-then-act pattern (`if uri in set` then `add`) creates TOCTOU race
- Can add same document twice or fail to remove it

**Solution - Add a Lock**:
```python
class LanguageServerClient:
    def __init__(self, ...):
        # ... existing code ...
        self._open_documents = set()
        self._open_documents_lock = threading.Lock()

    def textDocument_didOpen(self, params):
        textDocument_uri = params["textDocument"]["uri"]

        with self._open_documents_lock:
            if textDocument_uri in self._open_documents:
                return
            self._open_documents.add(textDocument_uri)

        self._put(notification("textDocument/didOpen", params))

    def textDocument_didClose(self, params):
        textDocument_uri = params["textDocument"]["uri"]

        with self._open_documents_lock:
            if textDocument_uri not in self._open_documents:
                return
            self._open_documents.remove(textDocument_uri)

        self._put(notification("textDocument/didClose", params))

    def textDocument_didChange(self, params):
        with self._open_documents_lock:
            if params["textDocument"]["uri"] not in self._open_documents:
                return

        self._put(notification("textDocument/didChange", params))
```

**Key Learning**:
- **Check-then-act** patterns are inherently racy - both operations must be atomic
- Keeping the entire sequence inside a lock ensures atomicity
- Sets are particularly dangerous because operations like `add()` can trigger resize

---

### 4. Server State Variables - Visibility Issues

**Location**: `smarts_client.py:437-440`

**Problem**:
```python
self._server_initializing = None
self._server_initialized = False
self._server_info: Optional[LSPServerInfo] = None
self._server_capabilities: Optional[dict] = None
```

**Why it's dangerous - Memory Visibility**:

Even with Python's GIL, there are **memory visibility** issues:
- Thread 1 writes a value to a variable
- Thread 2 reads that variable
- **Without synchronization**, Thread 2 might see a stale value (cached in CPU registers/cache)

Example:
```python
# Handler thread (in callback, line 744)
self._server_initialized = True  # Write

# Main thread (checking in _put, line 656)
if not self._server_initialized:  # Read - might see False!
    # Drop message incorrectly
```

**Why `_init_lock` isn't enough**:
The lock only protects the `initialize()` method call itself, but:
- State variables are **read** outside the lock (lines 459, 464, 473)
- State variables are **written** in callback without lock (line 744)
- No **happens-before** relationship between write and read

**Solution - Extend Lock Protection**:
```python
class LanguageServerClient:
    def __init__(self, ...):
        self._state_lock = threading.RLock()  # Reentrant lock
        # ... rest of init ...

    def is_server_initializing(self) -> Optional[bool]:
        with self._state_lock:
            return self._server_initializing

    def is_server_initialized(self) -> bool:
        with self._state_lock:
            return self._server_initialized

    def support_method(self, method: str) -> Optional[bool]:
        with self._state_lock:
            if not self._server_capabilities:
                return None

            # ... rest of method with _server_capabilities access ...

    def initialize(self, params, callback):
        with self._state_lock:
            if self._server_initializing or self._server_initialized:
                return
            self._server_initializing = True

        # ... start subprocess and threads ...

        def _callback(response: LSPResponseMessage):
            with self._state_lock:
                self._server_initializing = False

                if not response.get("error"):
                    self._server_initialized = True
                    result = response.get("result")

                    if result is not None:
                        self._server_capabilities = result.get("capabilities")
                        self._server_info = result.get("serverInfo")

            # Send initialized notification outside lock
            if self._server_initialized:
                self._put(notification("initialized", {}))

            callback(response)
```

**Alternative - Use threading.Event for Flags**:
```python
class LanguageServerClient:
    def __init__(self, ...):
        self._server_initialized_event = threading.Event()  # Thread-safe flag
        # ...

    def is_server_initialized(self) -> bool:
        return self._server_initialized_event.is_set()

    # In callback:
    def _callback(response):
        if not response.get("error"):
            self._server_initialized_event.set()  # Thread-safe
```

**Key Learning**:
- **Memory visibility** is separate from race conditions
- Locks provide both **mutual exclusion** AND **memory visibility** (happens-before guarantee)
- `threading.Event` is perfect for boolean flags shared across threads
- Use `RLock` (reentrant) when same thread might acquire lock multiple times

---

### 5. UI Operations from Background Threads

**Location**: Multiple places in `smarts.py`

**Problem**:
Background threads (handler thread) invoke callbacks that modify Sublime UI:

```python
# Line 1030 - Called from handler thread
handle_textDocument_publishDiagnostics(window, smart, notification)
    # Line 941-951 - Modifies window/view settings and UI
    window.settings().set(kDIAGNOSTICS, uri_diagnostics)
    view.settings().set(kDIAGNOSTICS, diagnostics)
    view.erase_regions(...)
    view.add_regions(...)
```

**Why it's dangerous**:
- Sublime Text's API is **not thread-safe**
- UI operations must run on **Sublime's main thread**
- Calling from background threads can cause:
  - Crashes
  - UI corruption
  - Deadlocks

**Solution - Use sublime.set_timeout()**:

Sublime Text provides `sublime.set_timeout()` to run code on the main thread:

```python
def on_receive_notification(
    smart_uuid: str,
    notification: smarts_client.LSPNotificationMessage,
):
    """This runs on handler thread - must delegate to main thread"""

    # Schedule to run on Sublime's main thread
    sublime.set_timeout(
        lambda: _handle_notification_main_thread(smart_uuid, notification),
        0  # Run ASAP
    )

def _handle_notification_main_thread(
    smart_uuid: str,
    notification: smarts_client.LSPNotificationMessage
):
    """This runs on main thread - safe to call Sublime API"""
    smart = find_smart(smart_uuid)

    if not smart:
        return

    window = find_window(smart["window"])

    if not window:
        return

    message_method = notification.get("method")

    if message_method == "$/logTrace":
        handle_logTrace(window, notification)
    elif message_method == "window/logMessage":
        handle_window_logMessage(window, notification)
    elif message_method == "window/showMessage":
        handle_window_showMessage(window, notification)
    elif message_method == "textDocument/publishDiagnostics":
        handle_textDocument_publishDiagnostics(window, smart, notification)
    else:
        panel_log(window, f"Unhandled Notification: {pprint.pformat(notification)}\n\n")
```

**For async variants**:
```python
# If you need to run async
sublime.set_timeout_async(lambda: some_async_operation(), 0)
```

**Key Learning**:
- **Never** call UI framework APIs from background threads
- Use framework-provided mechanisms to marshal calls to UI thread
- In Sublime: `sublime.set_timeout()` for sync, `sublime.set_timeout_async()` for async
- In other frameworks: Qt has `QMetaObject.invokeMethod()`, wxPython has `wx.CallAfter()`

---

## Medium Priority Issues

### 6. Queue Size Limitation

**Location**: `smarts_client.py:441-442`

**Problem**:
```python
self._send_queue = Queue(maxsize=1)
self._receive_queue = Queue(maxsize=1)
```

**Why it might be problematic**:
- `maxsize=1` means queue can only hold **one message**
- If handler/writer threads are slow, `put()` operations **block**
- Blocking could cause missed messages or UI freezes if called from main thread

**When this is OK**:
- If messages are processed faster than they arrive
- If blocking is acceptable in your use case

**Recommendation**:
Consider increasing to a reasonable size (e.g., 100) or removing limit:

```python
self._send_queue = Queue(maxsize=100)  # Buffer up to 100 messages
self._receive_queue = Queue(maxsize=100)
```

Or unbounded (use with caution):
```python
self._send_queue = Queue()  # Unlimited
```

**Trade-offs**:
- **Bounded queue**: Provides backpressure, prevents memory exhaustion, but can block
- **Unbounded queue**: Never blocks, but can grow infinitely if consumer is slow
- **Size of 1**: Minimizes memory but maximizes blocking

**Key Learning**:
- Queue size affects **throughput** vs **memory usage** vs **latency**
- For LSP, messages are typically small and fast - larger queue is usually fine
- Monitor queue sizes in production to tune appropriately

---

### 7. Timer Thread for Highlights

**Location**: `smarts.py:1860-1870`

**Problem**:
```python
highlighter = getattr(self, "pg_smarts_highlighter", None)

if highlighter and highlighter.is_alive():
    highlighter.cancel()
    self.pg_smarts_highlighter = threading.Timer(0.3, self.highlight)
    self.pg_smarts_highlighter.start()
```

**Potential race condition**:
- **Check** `is_alive()` and **cancel** are separate operations
- Between check and cancel, timer could fire
- Could result in multiple `highlight()` calls running simultaneously

**Solution - Always cancel regardless**:
```python
highlighter = getattr(self, "pg_smarts_highlighter", None)

# Always cancel if exists - cancel() on finished timer is safe
if highlighter:
    highlighter.cancel()

# Create new timer
self.pg_smarts_highlighter = threading.Timer(0.3, self.highlight)
self.pg_smarts_highlighter.start()
```

**Better alternative - Use a lock**:
```python
class PgSmartsViewListener(sublime_plugin.ViewEventListener):
    def __init__(self, view):
        super().__init__(view)
        self._highlight_lock = threading.Lock()
        self._highlighter = None

    def on_selection_modified_async(self):
        # ... existing checks ...

        with self._highlight_lock:
            if self._highlighter and self._highlighter.is_alive():
                self._highlighter.cancel()

            self._highlighter = threading.Timer(0.3, self.highlight)
            self._highlighter.start()
```

**Key Learning**:
- `Timer.cancel()` is **safe** to call on finished timers
- Simplify code by always canceling instead of checking first
- Protect timer creation/cancellation with a lock if multiple threads access it

---

## Best Practices Summary

### 1. **Identify Shared Mutable State**
Ask yourself:
- What data is accessed from multiple threads?
- Is any of it modified (not just read)?
- List all access points

### 2. **Choose the Right Synchronization Primitive**

| Use Case | Primitive | Example |
|----------|-----------|---------|
| Protecting data structures | `threading.Lock()` | Protecting `_SMARTS` list |
| Boolean flags | `threading.Event()` | `server_initialized` flag |
| Same thread acquiring twice | `threading.RLock()` | Nested method calls |
| Producer-consumer | `queue.Queue()` | Message passing (already used!) |
| Read-heavy workloads | `threading.RLock()` or reader-writer lock | Config data |

### 3. **Keep Critical Sections Small**
```python
# Bad - lock held during I/O
with lock:
    data = shared_dict.copy()
    process_data(data)  # Expensive operation
    write_to_disk(data)  # I/O operation

# Good - only protect shared access
with lock:
    data = shared_dict.copy()

# Process outside lock
process_data(data)
write_to_disk(data)
```

### 4. **Avoid Holding Multiple Locks**
Can cause deadlocks:
```python
# Thread 1
with lock_a:
    with lock_b:  # Deadlock if Thread 2 has lock_b
        # ...

# Thread 2
with lock_b:
    with lock_a:  # Deadlock if Thread 1 has lock_a
        # ...
```

**Solution**: Always acquire locks in the **same order**.

### 5. **Prefer Immutable Data**
```python
# Instead of shared mutable list
shared_list = []

# Use immutable tuple or return copies
with lock:
    snapshot = tuple(shared_list)  # Immutable snapshot

# Callers use snapshot without lock
for item in snapshot:
    process(item)
```

### 6. **Document Threading Assumptions**
```python
class LanguageServerClient:
    """
    Thread Safety:
    - _request_callback: Protected by _request_callback_lock
    - _open_documents: Protected by _open_documents_lock
    - _server_initialized: Use is_server_initialized() which acquires lock

    Threading Model:
    - Reader thread: Reads from server stdout
    - Writer thread: Writes to server stdin
    - Handler thread: Processes responses
    - Public methods: Called from Sublime main thread
    """
```

### 7. **Test for Races**
- Use threading stress tests
- Tools like Python's `threading.Thread` with many concurrent operations
- Consider `threading.Condition` for complex coordination

---

## Implementation Priority

### Phase 1 (Critical - Fix First)
1. ✅ Add lock for `_SMARTS` global list
2. ✅ Add lock for `_request_callback` dictionary
3. ✅ Fix UI operations from background threads (use `sublime.set_timeout()`)

### Phase 2 (Important)
4. ✅ Add lock for `_open_documents` set
5. ✅ Protect server state variables with proper synchronization

### Phase 3 (Nice to Have)
6. ⚡ Review queue sizes
7. ⚡ Improve timer thread handling

---

## Additional Resources

### Python Threading Documentation
- [threading module](https://docs.python.org/3/library/threading.html)
- [queue module](https://docs.python.org/3/library/queue.html)
- [Thread synchronization primitives](https://docs.python.org/3/library/threading.html#lock-objects)

### Concepts to Study
- **GIL (Global Interpreter Lock)**: Doesn't prevent all races!
- **Memory models**: Why locks provide visibility guarantees
- **Happens-before**: Ordering guarantees in concurrent code
- **Lock-free programming**: Advanced, usually not needed

### Debugging Threading Issues
```python
# Enable thread name logging
import threading
threading.current_thread().name = "MyThreadName"

# Log with thread info
import logging
logging.basicConfig(
    format='%(asctime)s [%(threadName)s] %(message)s',
    level=logging.DEBUG
)
```

---

## Questions for Further Learning

1. **Why doesn't Python's GIL prevent these issues?**
   - GIL protects Python interpreter internals, not your data structures
   - GIL releases during I/O operations
   - GIL doesn't guarantee memory visibility across threads

2. **When can I skip locks?**
   - Reading immutable data
   - Using thread-safe primitives (`Queue`, `Event`)
   - Single-writer, single-reader of primitive types (advanced, usually not worth it)

3. **What about async/await?**
   - Different concurrency model (cooperative multitasking)
   - Still need synchronization if mixing with threads
   - Sublime Text doesn't use async/await extensively

4. **How do I test this?**
   - Stress tests with many threads
   - ThreadSanitizer (C/C++ tool, some Python support)
   - Manual code review looking for shared mutable state

---

## Conclusion

Thread safety is about:
1. **Identifying** shared mutable state
2. **Protecting** it with appropriate primitives
3. **Minimizing** critical sections
4. **Documenting** threading assumptions
5. **Testing** thoroughly

The issues in this codebase are common and fixable. By systematically applying locks and using `sublime.set_timeout()` for UI operations, you'll have a robust, thread-safe LSP client.

Good luck with your improvements! 🚀
