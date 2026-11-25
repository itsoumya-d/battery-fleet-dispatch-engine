# battery-fleet-dispatch-engine

Distributed battery energy storage system (BESS) fleet control plane and dispatch optimization engine: manages site telemetry, state-of-charge (SOC) operating envelopes, command sequencing with idempotency guarantees, and automated fail-safe fallbacks across intermittent device connections.

## Architecture & Core Invariants

- **Fleet Site Management**: Tracks site configurations, nominal energy capacities (kWh), power limits (kW), and real-time state of charge (SOC).
- **Safety Envelope Enforcement**: Enforces hard battery safety thresholds (minimum 10% SOC, maximum 90% SOC, temperature limits) and rejects unsafe dispatch instructions.
- **Idempotent Command Sequencing**: Assigns monotonic sequence numbers and unique idempotency tokens to prevent double-dispatch during network retries.
- **Fail-Safe Fallbacks**: Automatically transitions sites to safe idle state if heartbeat telemetry expires or communication is degraded.

## Run Tests Locally

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

## CI/CD

Automated GitHub Actions workflow executes tests and code quality verification on push and pull requests.
