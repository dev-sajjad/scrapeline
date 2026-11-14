# ADR-0003: Job queue on PostgreSQL `FOR UPDATE SKIP LOCKED`

Status: accepted · Date: 2026-11-13

## Context
Workers need a queue for fetch/parse jobs. Scale target: thousands of jobs per day on one machine.

## Decision
Jobs live in `ops.job`. Workers claim with `SELECT ... FOR UPDATE SKIP LOCKED`, set a lease, and heartbeat. Expired leases are reclaimed. Dead-letter after N attempts.

## Alternatives
Redis/arq (second datastore), Redpanda/Kafka (operational weight far above the scale). Either may replace this via a new ADR if a measured bottleneck appears.

## Consequences
One datastore, transactional claims, trivially inspectable. Not suited to >10k jobs/min; that is outside scope.
