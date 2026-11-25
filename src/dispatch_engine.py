"""Distributed Battery Fleet Control Plane & Dispatch Optimization Engine.

Provides site configuration tracking, safety envelope enforcement (SOC and thermal limits),
idempotent command dispatch sequencing, and automated fail-safe fallbacks.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional
import time


class ConnectionStatus(str, Enum):
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"


class DispatchAction(str, Enum):
    CHARGE = "CHARGE"
    DISCHARGE = "DISCHARGE"
    IDLE = "IDLE"


@dataclass(frozen=True)
class SiteConfig:
    site_id: str
    nominal_energy_kwh: float
    max_charge_power_kw: float
    max_discharge_power_kw: float
    min_soc_pct: float = 10.0
    max_soc_pct: float = 90.0
    max_cell_temp_c: float = 45.0


@dataclass(frozen=True)
class TelemetryPoint:
    site_id: str
    current_soc_pct: float
    active_power_kw: float
    cell_temperature_c: float
    timestamp: float


@dataclass(frozen=True)
class DispatchCommand:
    command_id: str
    site_id: str
    action: DispatchAction
    target_power_kw: float
    duration_seconds: int
    sequence_number: int
    idempotency_token: str
    created_at: float


@dataclass
class BatterySiteState:
    config: SiteConfig
    status: ConnectionStatus = ConnectionStatus.ONLINE
    current_soc_pct: float = 50.0
    active_power_kw: float = 0.0
    cell_temperature_c: float = 25.0
    last_heartbeat: float = 0.0
    last_sequence_number: int = 0
    emergency_lockout: bool = False
    lockout_reason: str = ""


class FleetControlPlane:
    """Manages BESS fleet sites, health telemetry, and safe dispatch command sequencing."""

    def __init__(self, heartbeat_timeout_seconds: float = 60.0):
        self.heartbeat_timeout = heartbeat_timeout_seconds
        self._sites: Dict[str, BatterySiteState] = {}
        self._idempotency_cache: Dict[str, DispatchCommand] = {}
        self._command_history: List[DispatchCommand] = []

    def register_site(self, config: SiteConfig) -> None:
        if config.nominal_energy_kwh <= 0 or config.max_charge_power_kw <= 0 or config.max_discharge_power_kw <= 0:
            raise ValueError("Site capacities and power ratings must be strictly positive")
        self._sites[config.site_id] = BatterySiteState(config=config, last_heartbeat=time.time())

    def get_site(self, site_id: str) -> BatterySiteState:
        if site_id not in self._sites:
            raise KeyError(f"Site {site_id} is not registered in fleet control plane")
        return self._sites[site_id]

    def ingest_telemetry(self, telemetry: TelemetryPoint) -> None:
        site = self.get_site(telemetry.site_id)
        site.last_heartbeat = telemetry.timestamp
        site.current_soc_pct = telemetry.current_soc_pct
        site.active_power_kw = telemetry.active_power_kw
        site.cell_temperature_c = telemetry.cell_temperature_c
        site.status = ConnectionStatus.ONLINE

        # Safety: thermal limits
        if telemetry.cell_temperature_c >= site.config.max_cell_temp_c:
            site.emergency_lockout = True
            site.lockout_reason = (
                f"Cell temperature {telemetry.cell_temperature_c}C exceeds safety limit {site.config.max_cell_temp_c}C"
            )

    def evaluate_heartbeats(self, current_time: Optional[float] = None) -> List[str]:
        """Detects stale sites and downgrades connection status to OFFLINE."""
        now = current_time if current_time is not None else time.time()
        offline_sites = []
        for site_id, site in self._sites.items():
            if now - site.last_heartbeat > self.heartbeat_timeout:
                if site.status != ConnectionStatus.OFFLINE:
                    site.status = ConnectionStatus.OFFLINE
                    site.active_power_kw = 0.0
                offline_sites.append(site_id)
        return offline_sites

    def schedule_dispatch(
        self,
        site_id: str,
        action: DispatchAction,
        target_power_kw: float,
        duration_seconds: int,
        idempotency_token: str,
    ) -> DispatchCommand:
        """Validates safety invariants and creates an ordered dispatch command."""
        # Idempotency check
        if idempotency_token in self._idempotency_cache:
            return self._idempotency_cache[idempotency_token]

        site = self.get_site(site_id)

        if site.status == ConnectionStatus.OFFLINE:
            raise ConnectionError(f"Cannot dispatch commands to OFFLINE site {site_id}")

        if site.emergency_lockout:
            raise PermissionError(f"Site {site_id} is in emergency safety lockout: {site.lockout_reason}")

        if duration_seconds <= 0:
            raise ValueError("Dispatch duration must be greater than zero")

        clamped_power = max(0.0, target_power_kw)

        if action == DispatchAction.CHARGE:
            if site.current_soc_pct >= site.config.max_soc_pct:
                raise ValueError(
                    f"Site {site_id} SOC ({site.current_soc_pct}%) already at or above maximum ({site.config.max_soc_pct}%)"
                )
            clamped_power = min(clamped_power, site.config.max_charge_power_kw)

        elif action == DispatchAction.DISCHARGE:
            if site.current_soc_pct <= site.config.min_soc_pct:
                raise ValueError(
                    f"Site {site_id} SOC ({site.current_soc_pct}%) already at or below minimum ({site.config.min_soc_pct}%)"
                )
            clamped_power = min(clamped_power, site.config.max_discharge_power_kw)

        elif action == DispatchAction.IDLE:
            clamped_power = 0.0

        site.last_sequence_number += 1
        cmd = DispatchCommand(
            command_id=f"cmd-{site_id}-{site.last_sequence_number}",
            site_id=site_id,
            action=action,
            target_power_kw=clamped_power,
            duration_seconds=duration_seconds,
            sequence_number=site.last_sequence_number,
            idempotency_token=idempotency_token,
            created_at=time.time(),
        )

        self._idempotency_cache[idempotency_token] = cmd
        self._command_history.append(cmd)
        return cmd
