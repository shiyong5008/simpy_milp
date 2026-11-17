from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from fab_sim.config import OperationConfig
from fab_sim.models import Lot, Tool


@dataclass(frozen=True)
class BatchDecision:
    lot_count: int
    tool: Tool
    estimated_start: float
    queue_time: float
    queue_time_violations: int = 0


class MilpBatchScheduler:
    def __init__(self, min_batch_lots: int, max_batch_lots: int) -> None:
        if min_batch_lots <= 0 or max_batch_lots < min_batch_lots:
            raise ValueError("Invalid batch lot limits")
        self.min_batch_lots = min_batch_lots
        self.max_batch_lots = max_batch_lots

    def choose_batch(
        self,
        lots: list[Lot],
        tools: list[Tool],
        operation: OperationConfig,
        now: float,
        *,
        max_outstanding_batches: int,
        stage_time_mins: float = 0.0,
    ) -> BatchDecision | None:
        lot_count = min(len(lots), self.max_batch_lots)
        if lot_count < self.min_batch_lots:
            return None

        batch = lots[:lot_count]
        entry_times = [
            lot.operation_entry_times.get(
                operation.ops_id, now + stage_time_mins
            )
            for lot in batch
        ]
        operation_entry_time = max(entry_times, default=now + stage_time_mins)
        oldest_entry_time = min(entry_times, default=operation_entry_time)

        candidates: list[tuple[Tool, float, float, int]] = []
        for tool in tools:
            if tool.operation_config.ops_id != operation.ops_id:
                continue
            if tool.wip_batches >= max_outstanding_batches:
                continue

            estimated_start = self._estimated_start(tool, now, operation_entry_time)
            queue_time = estimated_start - oldest_entry_time
            if queue_time > operation.max_q_time_mins:
                continue

            candidates.append((tool, estimated_start, queue_time, tool.wip_batches))

        if not candidates:
            return None

        # Minimize the candidate's projected completion time. Current WIP
        # breaks close ties toward the less-loaded tool.
        costs = np.array(
            [
                start
                + tool.operation_config.proc_time_per_load_mins
                + wip * 1e-3
                + index * 1e-6
                for index, (tool, start, _, wip) in enumerate(candidates)
            ],
            dtype=float,
        )
        variable_count = len(candidates)
        objective_row = np.ones((1, variable_count), dtype=float)
        capacity_rows = np.eye(variable_count, dtype=float)
        capacity_upper = np.array(
            [
                max_outstanding_batches - wip
                for _, _, _, wip in candidates
            ],
            dtype=float,
        )
        qtime_rows = np.diag([queue_time for _, _, queue_time, _ in candidates])
        qtime_upper = np.full(variable_count, operation.max_q_time_mins)
        constraints = LinearConstraint(
            np.vstack((objective_row, capacity_rows, qtime_rows)),
            np.concatenate(([1.0], np.full(variable_count, -np.inf),
                            np.full(variable_count, -np.inf))),
            np.concatenate(([1.0], capacity_upper, qtime_upper)),
        )
        result = milp(
            c=costs,
            integrality=np.ones(variable_count, dtype=int),
            bounds=Bounds(np.zeros(variable_count), np.ones(variable_count)),
            constraints=constraints,
        )
        if not result.success or result.x is None:
            raise RuntimeError(
                f"MILP batch assignment failed: {result.message}"
            )

        selected = int(np.argmax(result.x))
        tool, estimated_start, queue_time, _ = candidates[selected]
        return BatchDecision(
            lot_count=lot_count,
            tool=tool,
            estimated_start=estimated_start,
            queue_time=queue_time,
        )

    @staticmethod
    def _estimated_start(tool: Tool, now: float, entry_time: float) -> float:
        processing_time = tool.operation_config.proc_time_per_load_mins
        pm_duration = tool.config.pm_duration_mins
        run_limit = tool.config.pm_run_count
        time_limit = tool.config.pm_mins_count

        if tool.pm_active:
            time = max(now, tool.pm_completion_at or now)
            last_pm_completion = time
            runs_since_pm = 0
            pm_pending = False
        else:
            time = now
            last_pm_completion = tool.last_pm_completion
            runs_since_pm = tool.run_count_since_pm
            pm_pending = tool.pm_pending

        active = tool.current_batch_end_time is not None
        queued_batches = max(0, tool.wip_batches - int(active))

        if active:
            active_end = max(time, tool.current_batch_end_time or time)
            runs_since_pm += 1
            time_due = (
                tool.config.time_count_flag
                and active_end >= last_pm_completion + time_limit
            )
            run_due = (
                tool.config.run_count_flag
                and runs_since_pm >= run_limit
            )
            pm_pending = pm_pending or time_due or run_due
            time = active_end

        for _ in range(queued_batches):
            time, last_pm_completion, runs_since_pm, pm_pending = (
                MilpBatchScheduler._apply_due_pm(
                    tool,
                    time,
                    last_pm_completion,
                    runs_since_pm,
                    pm_pending,
                )
            )
            batch_end = time + processing_time
            runs_since_pm += 1
            time_due = (
                tool.config.time_count_flag
                and batch_end >= last_pm_completion + time_limit
            )
            run_due = (
                tool.config.run_count_flag
                and runs_since_pm >= run_limit
            )
            pm_pending = time_due or run_due
            time = batch_end

        time = max(time, entry_time)
        time, last_pm_completion, runs_since_pm, _ = (
            MilpBatchScheduler._apply_due_pm(
                tool,
                time,
                last_pm_completion,
                runs_since_pm,
                pm_pending,
            )
        )
        return max(time, entry_time)

    @staticmethod
    def _apply_due_pm(
        tool: Tool,
        time: float,
        last_pm_completion: float,
        runs_since_pm: int,
        pm_pending: bool,
    ) -> tuple[float, float, int, bool]:
        time_due = (
            tool.config.time_count_flag
            and time >= last_pm_completion + tool.config.pm_mins_count
        )
        run_due = (
            tool.config.run_count_flag
            and runs_since_pm >= tool.config.pm_run_count
        )
        if pm_pending or time_due or run_due:
            time += tool.config.pm_duration_mins
            return time, time, 0, False
        return time, last_pm_completion, runs_since_pm, False
