# LSP Client Resilience Improvements

This document explains the resilience improvements made to `smarts_client.py` and the rationale behind each change.

## Summary

Made the LSP client more robust by:
1. Adding explicit failure state tracking
2. Handling initialization failures
3. Adding exception handling to I/O threads
4. Properly handling pipe errors
5. Monitoring server process for crashes
6. Cleaning up resources on failure
7. Documenting known limitations

## Detailed Changes

### 1. Added FAILED State to ServerStatus Enum

**What Changed:**
- Added `FAILED` enum value to `ServerStatus`
- Added `is_server_failed()` method

**Why:**
Without a FAILED state, when initialization failed or the server crashed, the client would stay in INITIALIZING or INITIALIZED state forever. This led to confusing behavior where:
- The client appeared to be working but didn't accept requests
- No clear indication that something went wrong
- Higher-level code couldn't detect and handle failures

**Impact:**
- Explicit failure detection
- Clear state transitions
- Enables retry logic in calling code

### 2. Handle Initialization Failures

**What Changed:**
- Modified initialize() callback to transition to FAILED when server returns an error
- Log detailed error information from the server

**Why:**
Previously, if the server rejected initialization (e.g., invalid config, unsupported version), the client would:
- Stay in INITIALIZING state forever
- Not log the specific error from the server
- Block all future operations silently

**Impact:**
- Clear error messages when initialization fails
- Client state accurately reflects reality
- Callers can detect failure and retry or alert user

### 3. Added Exception Handling to Reader Thread

**What Changed:**
- Wrapped reader loop in try-except
- Transition to FAILED on I/O errors
- Signal other threads to stop gracefully

**Why:**
The reader performs blocking I/O (readline, read) that can raise exceptions if:
- Server subprocess crashes
- stdout pipe is closed unexpectedly
- Encoding errors occur

Without exception handling, the reader thread would crash silently, leaving the client in an inconsistent state where it thinks the server is running but can't receive messages.

**Impact:**
- Graceful handling of server crashes
- Clean shutdown of worker threads
- Clear error logging

### 4. Fixed BrokenPipeError Handling in Writer

**What Changed:**
- Catch BrokenPipeError specifically
- Transition to FAILED state
- Stop the writer thread (break the loop)
- Properly manage queue.task_done() calls

**Why:**
Previously, when the server crashed and stdin pipe broke:
- Writer would log error but continue looping
- Would repeatedly try to write to broken pipe
- Spam error logs
- Waste CPU cycles

The new code:
- Recognizes pipe break as fatal error
- Stops cleanly
- Signals other threads
- Prevents error log spam

**Impact:**
- Clean shutdown on pipe errors
- No repeated error messages
- Proper resource cleanup

### 5. Added Process Monitoring Thread

**What Changed:**
- Created `_start_monitor()` method
- Thread blocks on `process.wait()`
- Detects unexpected exits vs. normal shutdown
- Transitions to FAILED on crashes

**Why:**
Without process monitoring, if the server crashed (segfault, killed by OS, etc.):
- Client wouldn't know until trying to do I/O
- Could hang waiting for responses
- Confusing delay before error detection

The monitor thread:
- Detects crashes immediately
- Distinguishes expected vs unexpected exits
- Triggers clean shutdown promptly

**Impact:**
- Immediate crash detection
- Faster failure response
- Better user experience

### 6. Clear Pending Callbacks on Failure

**What Changed:**
- Created `_clear_pending_callbacks_locked()` method
- Call it whenever transitioning to FAILED
- Log number of cleared callbacks

**Why:**
When server fails, in-flight requests never receive responses. Without clearing callbacks:
1. **Memory leak** - callbacks stay in dictionary forever
2. **Confusion** - callers might wait indefinitely for responses

By explicitly clearing callbacks:
- Free memory
- Make failure mode explicit
- Callers see no callback invoked (can implement timeouts)

**Impact:**
- No memory leaks
- Clear failure semantics
- Better resource management

### 7. Documented Known Limitations

**What Changed:**
- Added comprehensive class docstring
- Documented thread safety guarantees
- Listed resilience features
- Explicitly called out limitations

**Why:**
Important for maintainers and users to understand:
- What the client handles automatically
- What limitations exist
- Where callers need to implement their own logic (timeouts, retries)

Documented limitations:
1. Reader can hang on incomplete messages (Python I/O limitation)
2. No automatic reconnection (design choice)
3. No callback timeouts (caller responsibility)
4. Queue size limits create backpressure (intentional)

**Impact:**
- Clear expectations for users
- Easier maintenance
- Prevents misuse

## Testing Recommendations

To verify these improvements work correctly, test:

1. **Initialization failure**: Start server with invalid config
   - Should transition to FAILED
   - Should log error details

2. **Server crash during operation**: Kill server process (kill -9)
   - Monitor should detect immediately
   - Should transition to FAILED
   - Should clear pending callbacks

3. **Broken pipe**: Close server stdin externally
   - Writer should detect BrokenPipeError
   - Should transition to FAILED and stop cleanly

4. **Reader I/O error**: Corrupt server output
   - Reader should catch exception
   - Should transition to FAILED

5. **Multiple failures**: Trigger multiple failure conditions simultaneously
   - Should only transition to FAILED once
   - Shouldn't cause races or crashes

## Migration Notes

These changes are **backward compatible**:
- No API changes
- Existing code continues to work
- New `is_server_failed()` method is optional to use

Recommended upgrades for calling code:
- Check `is_server_failed()` to detect failures
- Implement retry logic when initialization fails
- Add timeout logic for request callbacks
