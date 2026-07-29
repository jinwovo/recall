package com.portfolio.recall.admin;

/**
 * Outcome of a bounded DLQ drain (docs/adr/0006): {@code replayed} records were re-published
 * to the ingestion topic and their offsets committed; {@code remaining} is the backlog still
 * waiting (as of the drain's start snapshot — a replayed poison pill that immediately
 * dead-letters again shows up on the next inspect, not here).
 */
public record DlqReplayResult(int replayed, long remaining) {}
