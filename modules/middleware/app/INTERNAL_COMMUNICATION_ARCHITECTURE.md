# Internal Communication Architecture

## Scope

This architecture covers internal module communication, event persistence, retry, dead-letter handling, and operational visibility for the Omni-Core middleware.

## Event Model

The unified internal event uses a versioned event schema:

- Event ID
- Event type
- Timestamp
- Source module
- Target module
- Entity ID
- Correlation ID
- Trace ID
- Schema version
- Payload
- Processing status
- Retry count
- Maximum retries
- Error message
- Headers

## Event Lifecycle

1. Publisher emits an event.
2. EventBus assigns an event ID and UTC timestamp.
3. Event is persisted or queued.
4. Worker dispatches to subscribers.
5. Subscriber failures increment retry count.
6. Retries use exponential backoff.
7. Terminal failure moves event to dead-letter queue.
8. Replay resets pending state and requeues event.

## Implementation Status

- Event bus implemented with topic-based subscriptions.
- In-process queue with bounded size.
- Dead-letter queue and replay implemented.
- Retry handling implemented.
- Subscriber health tracking implemented.
- Event statistics implemented.
- Structured logging used for event lifecycle updates.

## Management Surfaces

- Event monitor: event bus statistics.
- Queue monitor: queue size and dead-letter size.
- Module communication: source and target module metadata.
- Event history: persisted event logs.
- Failed messages: dead-letter queue.
- Retry control: replay dead-lettered events.
- Dependency graph: module list and event routing metadata.
- Communication latency and throughput: event statistics and subscriber health.
- Error rates: failed and dead-lettered event counters.

## Gaps

- Dedicated event persistence store instead of system-log-based persistence.
- Schema validation and idempotency enforcement are not yet wired into publish.
- Module-level dependency graph is not fully modeled.
- Operational dashboards are partially integrated through Global Control Center.

## Recommendation

Keep the current custom event bus as the internal communication backbone and extend it with validation, idempotency, and management APIs before introducing an external message broker.

