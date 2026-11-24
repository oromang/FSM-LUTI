"""Dependency-free terminal view of the run's structured progress events.

The complete event history belongs in ``progress.jsonl``. This display is a
throttled view, not a convergence monitor or an estimate of remaining runtime.
Only the parent process should own it; worker processes send event dictionaries
to the parent. Calls from parent threads are serialized.
"""
from __future__ import annotations

import math
import shutil
import sys
import threading
import time


def _text(value):
    return " ".join("".join(c if c.isprintable() else " " for c in str(value)).split())


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    return f"{number:.3g}" if math.isfinite(number) else "?"


def _count(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


class TerminalProgress:
    """Callable progress sink with an ASCII progress bar on stderr.

    TTYs update one line. Redirected logs receive complete milestone lines and
    at most one ordinary iteration update per ``log_interval`` seconds.
    ``run_plan`` may supply ``total_cases`` and ``total_years``. Scenario/case,
    year and transport ``state`` tags identify concurrent work without relying
    on the order in which workers produce events. Counts represent completed
    cases/dates, not wall-clock work or an ETA.
    """

    _milestones = frozenset({
        "run_start", "run_plan", "run_done", "run_failed", "scenario_start",
        "scenario_done", "case_start", "case_done", "year_start", "year_done",
        "transport_start", "transport_done", "appraisal_blocked", "export_start",
        "export_done", "validation_start", "validation_done", "worker_start",
        "preflight_start", "preflight_stage", "preflight_done",
        "execution_plan", "luti_iteration_start", "luti_iteration",
        "luti_final_check", "luti_done", "luti_not_converged",
    })

    def __init__(self, stream=None, *, enabled=True, min_interval=.5,
                 log_interval=10., total_cases=None, clock=None):
        if min_interval < 0 or log_interval < 0:
            raise ValueError("Progress refresh intervals must be nonnegative")
        self.stream = sys.stderr if stream is None else stream
        self.enabled = bool(enabled)
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.min_interval = float(min_interval)
        self.log_interval = float(log_interval)
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.last_write = -math.inf
        self.total_cases = _count(total_cases)
        self.total_years = 0
        self.completed_cases = set()
        self.completed_years = set()
        self.active_cases = set()
        self.contexts = {}
        self.last_context = {}
        self.last_line = ""
        self.line_visible = False
        self.closed = False
        self.lock = threading.RLock()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def __call__(self, row):
        if not self.enabled:
            return
        with self.lock:
            if self.closed:
                return
            event = str(row.get("event", row.get("stage", "")))
            if "total_cases" in row:
                self.total_cases = _count(row["total_cases"])
            if "total_years" in row:
                self.total_years = _count(row["total_years"])
            key = (row.get("scenario"), row.get("case"))
            if any(value is not None for value in key):
                context = self.contexts.setdefault(key, {})
                if "year" in row and context.get("year") != row["year"]:
                    context.pop("luti", None)
                context.update({k: row[k] for k in ("scenario", "case", "year", "state") if k in row})
                # A new year must not inherit the previous transport state.
                if event in {"case_start", "year_start"}:
                    context.pop("state", None)
                    context.pop("luti", None)
                if event == "case_start":
                    context.pop("year", None)
                    self.active_cases.add(key)
            elif event.startswith(("run_", "validation_", "preflight_", "scenario_")):
                context = {k: row[k] for k in ("scenario", "year", "state") if k in row}
            else:
                # Backward-compatible serial callbacks; parallel callbacks must
                # carry their own scenario/case tags.
                context = {**self.last_context, **{k: row[k] for k in ("year", "state") if k in row}}
                if (event in {"case_start", "year_start"}
                        or ("year" in row and self.last_context.get("year") != row["year"])):
                    context.pop("luti", None)
            self._update_luti(context, event, row)
            if not event.startswith(("run_", "validation_", "preflight_", "scenario_")):
                self.last_context = context.copy()
            if event == "case_done":
                self.completed_cases.add(key)
                self.active_cases.discard(key)
            if event == "year_done":
                self.completed_years.add((*key, row.get("year")))
            now = self.clock()
            elapsed = max(0, int(now - self.started))
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)
            duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            prefix = self._bar()
            location = " / ".join(_text(context[k]) for k in ("scenario", "case", "year", "state") if context.get(k) is not None)
            activity = self._activity(event, row, context)
            active = f"{len(self.active_cases)} active" if len(self.active_cases) > 1 else ""
            self.last_line = " | ".join(part for part in (prefix, active, location, activity, duration) if part)
            important = (event in self._milestones or "warning" in row
                         or row.get("coupling_converged") is False)
            interval = self.min_interval if self.tty else self.log_interval
            if important or now - self.last_write >= interval:
                self._write(self.last_line)
                self.last_write = now
            if event in {"run_done", "run_failed"}:
                self.close()

    def _bar(self):
        if self.total_cases:
            count, total, unit = len(self.completed_cases), self.total_cases, "cases"
        elif self.total_years:
            count, total, unit = len(self.completed_years), self.total_years, "dates"
        else:
            return "[working]"
        filled = min(16, int(16 * count / total))
        bar = f"[{'#' * filled}{'-' * (16 - filled)}] {count}/{total} {unit}"
        if unit == "cases" and self.total_years:
            bar += f"; {len(self.completed_years)}/{self.total_years} dates"
        return bar

    @staticmethod
    def _update_luti(context, event, row):
        """Keep the outer LUTI loop visible while its inner solvers run."""
        if context.get("state") in {"reference", "fixed"}:
            context.pop("luti", None)
        if not (event.startswith("luti_") or row.get("stage") == "luti"):
            return
        luti = context.setdefault("luti", {})
        aliases = {
            "iteration": ("iteration", "iterations"),
            "max_iterations": ("max_iterations",),
            "residual": ("population_relative_residual", "population_residual", "relative_residual", "residual"),
            "tolerance": ("tolerance",),
            "residual_abs_people": ("residual_abs_people",),
            "max_zone_abs_residual_people": ("max_zone_abs_residual_people",),
            "tolerance_abs_people": ("tolerance_abs_people",),
            "converged": ("converged", "coupling_converged"),
        }
        for target, keys in aliases.items():
            for key in keys:
                if key in row:
                    luti[target] = row[key]
                    break
        if event == "luti_iteration_start":
            luti["final_check"] = False
        elif event == "luti_final_check":
            luti["final_check"] = True

    @staticmethod
    def _luti_label(luti):
        iteration = _text(luti.get("iteration", "?"))
        if "max_iterations" in luti:
            iteration += "/" + _text(luti["max_iterations"])
        if luti.get("final_check"):
            return f"LUTI final check ({iteration})"
        return f"LUTI {iteration}"

    @classmethod
    def _activity(cls, event, row, context=None):
        luti = (context or {}).get("luti", {})
        luti_event = event.startswith("luti_") or row.get("stage") == "luti"
        if "warning" in row and not luti_event:
            return "WARNING: " + _text(row["warning"])
        if event == "run_failed":
            return "FAILED: " + _text(row.get("error", "see progress.jsonl"))
        if event == "appraisal_blocked":
            return "APPRAISAL BLOCKED: " + _text(row.get("reason", "transport did not converge"))
        if event == "execution_plan":
            requested = _text(row.get('requested_case_workers', '?'))
            effective = _text(row.get('effective_case_workers', '?'))
            routing = _text(row.get('assignment_workers_per_case', '?'))
            memory = _number(row.get('available_memory_gb'))
            return (f"project workers {effective}/{requested} requested, routing {routing}/case; "
                    f"available RAM {memory} GiB; RAM/CPU case limits "
                    f"{_text(row.get('memory_limit', '?'))}/{_text(row.get('cpu_limit', '?'))}")
        if event == "road_assignment" or row.get("stage") == "road_assignment":
            inner = _text(row.get("iteration", "?"))
            if "max_iterations" in row:
                inner += "/" + _text(row["max_iterations"])
            outer = _text(row.get("outer_iteration", "?"))
            if "max_outer_iterations" in row:
                outer += "/" + _text(row["max_outer_iterations"])
            target = row.get("relative_gap_target", row.get("assignment_relative_gap_target"))
            suffix = f" (target {_number(target)})" if target is not None else ""
            prefix = cls._luti_label(luti) + "; " if luti else ""
            return f"{prefix}road {inner}, feedback {outer}, gap {_number(row.get('relative_gap'))}{suffix}"
        if "modal_total_variation" in row:
            limit = "/" + _text(row["max_outer_iterations"]) if "max_outer_iterations" in row else ""
            target = f" (target {_number(row['demand_tolerance'])})" if "demand_tolerance" in row else ""
            prefix = cls._luti_label(luti) + "; " if luti else ""
            return (f"{prefix}feedback {_text(row.get('iteration', '?'))}{limit}, modal change "
                    f"{_number(row['modal_total_variation'])}{target}, road gap "
                    f"{_number(row.get('assignment_relative_gap'))}")
        if luti_event:
            residual = luti.get("residual")
            detail = f", population residual {_number(residual)}" if residual is not None else ""
            if "tolerance" in luti:
                detail += f" (target {_number(luti['tolerance'])})"
            if "residual_abs_people" in luti:
                detail += f", summed zonal error {_number(luti['residual_abs_people'])} people"
                if "tolerance_abs_people" in luti:
                    detail += f" (target {_number(luti['tolerance_abs_people'])})"
            if "max_zone_abs_residual_people" in luti:
                detail += f", worst zone {_number(luti['max_zone_abs_residual_people'])} people"
            if event == 'luti_iteration_start':
                detail += ', solving transport'
            label = cls._luti_label(luti)
            if luti.get("converged") is False or event == "luti_not_converged":
                label = "WARNING: LUTI NOT CONVERGED; " + label
            elif event == "luti_done" and luti.get("converged") is True:
                label += " converged"
            warning = "; " + _text(row["warning"]) if "warning" in row else ""
            return label + detail + warning
        labels = {"transport_start": "solving transport", "transport_done": "transport complete",
                  "transport_cache_hit": "reusing identical transport", "year_start": "starting date",
                  "year_done": "date complete", "case_start": "starting case", "case_done": "case complete",
                  "run_start": "starting run", "run_plan": "run planned", "run_done": "run complete",
                  "validation_start": "validating inputs", "validation_done": "inputs validated",
                  "preflight_start": "checking run configuration", "preflight_stage": "checking inputs",
                  "preflight_done": "preflight complete",
                  "scenario_start": "starting scenario", "scenario_done": "scenario complete",
                  "export_start": "saving outputs", "export_done": "outputs saved"}
        label = labels.get(event, _text(event.replace("_", " ")) or "working")
        if row.get("converged") is False or row.get("coupling_converged") is False:
            label += " (NOT CONVERGED)"
        if event == "run_done" and row.get("all_transport_converged") is False:
            label += " (transport convergence incomplete)"
        if event == "run_done" and row.get("all_numerical_coupling_converged") is False:
            label += " (LUTI convergence incomplete; inspect approximation diagnostics)"
        if "message" in row:
            label += ": " + _text(row["message"])
        return label

    def _write(self, line):
        if self.tty:
            width = max(20, shutil.get_terminal_size(fallback=(140, 24)).columns - 1)
            # No ANSI sequences: compatible with ordinary Windows consoles.
            parts = line.split(" | ")
            if len(line) > width and len(parts) > 2:
                parts.pop()  # Iteration details matter more than elapsed time.
                parts[0] = parts[0].replace("################", "########").replace("----------------", "--------")
                if len(" | ".join(parts)) > width and parts[0].startswith("["):
                    parts[0] = parts[0].partition("] ")[2] or parts[0]
                # Keep the numerical solver details, shortening the location
                # only when the console cannot fit the complete case name.
                if len(parts) >= 3:
                    location_index = len(parts) - 2
                    excess = len(" | ".join(parts)) - width
                    location = parts[location_index]
                    if excess > 0 and len(location) > 12:
                        keep = max(9, len(location) - excess - 3)
                        parts[location_index] = location[:keep] + "..."
                if len(" | ".join(parts)) > width:
                    parts[0] = parts[0].replace(" cases; ", "c ").replace(" cases", "c").replace(" dates", "d")
                if len(" | ".join(parts)) > width:
                    parts[-1] = (parts[-1].replace("feedback ", "fb ")
                                 .replace(" (target ", " (<= ")
                                 .replace("population residual ", "pop residual ")
                                 .replace("summed zonal error ", "L1 ")
                                 .replace(" people", "p"))
                line = " | ".join(parts)
            clipped = line[:width]
            self.stream.write("\r" + clipped.ljust(width))
            self.line_visible = True
        else:
            self.stream.write(line + "\n")
        self.stream.flush()

    def break_line(self):
        """Allow a normal stdout message without overwriting the current bar."""
        with self.lock:
            if self.line_visible:
                self.stream.write("\n")
                self.stream.flush()
                self.line_visible = False

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.break_line()
            self.closed = True
