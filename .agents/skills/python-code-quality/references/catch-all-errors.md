# Catch-All Error Handling

Replace failure handling that hides unexpected input or loses operational evidence.

## Find

- bare `except`, `except BaseException`, and broad `except Exception`
- `asyncio.gather(..., return_exceptions=True)` results that are not inspected
- unknown branches handled with `pass` or `continue`
- masking defaults such as `payload.get("key", "")` where the default becomes valid data
- long-running loops that catch, log weakly, and continue

## Refactor

1. Identify expected failures and handle their narrow exception types.
2. Let unexpected failures propagate unless a long-running boundary must remain alive.
3. In a resilient loop, send a typed, bounded error to the system's real observability or health surface before continuing.
4. Validate unknown payload variants at ingestion and reject unsupported shapes.
5. Preserve cancellation and termination semantics; do not swallow `asyncio.CancelledError` or `BaseException`.
6. Add tests that prove failure is visible and carries useful context.

Error context should identify the subsystem and operation without exposing secrets or unbounded payloads.
