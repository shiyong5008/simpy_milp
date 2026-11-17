from __future__ import annotations

from dataclasses import dataclass, field

import simpy

from fab_sim.config import ToolConfig, ToolOperationConfig


@dataclass
class Lot:
    lot_id: str
    wafer_count: int
    status: str = "active"
    operation_entry_times: dict[int, float] = field(default_factory=dict)
    operation_queue_times: dict[int, float] = field(default_factory=dict)


@dataclass
class Tool:
    config: ToolConfig
    operation_config: ToolOperationConfig
    resource: simpy.PriorityResource
    batch_queue: simpy.Store
    wip_batches: int = 0
    pm_pending: bool = False
    pm_active: bool = False
    run_count_since_pm: int = 0
    last_pm_completion: float = 0.0
    pm_completion_at: float | None = None
    current_batch_end_time: float | None = None
