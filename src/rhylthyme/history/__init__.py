"""
Execution history: run records written by the CLI runner.

* ``hash``       — canonical program hash (``programVersion``)
* ``recorder``   — ``RunRecorder`` event listener that builds and writes records
* ``store``      — on-disk layout, listing, lookup and schema validation
* ``replay``     — re-resolve a program's triggers against the observed ends
* ``usable``     — the shared usable-run filter (JS twin: ``mcp-api/history.js``)
* ``report``     — the inferentiality report (per-step verdicts)
* ``evaluate``   — held-out evaluation: predicted vs planned mean absolute error
* ``synth``      — synthetic corpora with known generating functions
* ``factors``    — declared variance factors and the run-start prompt
* ``predict``    — history-based duration prediction (a byte-identical copy of
  ``rhylthyme_server/rhylthyme/predict.py``; JS twin: ``mcp-api/history.js``)
* ``calibrate``  — duration proposals from history (never writes) and acceptance
The commands (``rhylthyme runs``, ``runs report``, ``runs evaluate``,
``calibrate``) and the planned-vs-actual SVG renderer, which needs Node.js,
live in :mod:`rhylthyme.cli.history`.
"""

from .calibrate import (
    CalibrationProposal,
    StepProposal,
    apply_calibration,
    calibration_effect,
    current_values,
    propose_calibration,
)
from .evaluate import (
    BASIS_ORDER,
    DEFAULT_HOLDOUT,
    DEFAULT_MIN_TOTAL_RUNS,
    Evaluation,
    GroupEvaluation,
    StepEvaluation,
    evaluate_all,
    evaluate_program,
    group_by_program,
    program_stub_from_records,
    sort_records,
    split_chronologically,
)
from .factors import (
    coerce_factor_value,
    collect_factors,
    declared_factors,
    factors_from_env,
    parse_factor_args,
    preset_answers,
    prompt_for_factors,
)
from .hash import canonical_json, load_program_for_hash, program_version
from .predict import (
    BASIS_IDENTICAL,
    BASIS_MODEL,
    BASIS_NONE,
    DEFAULT_CORR_THRESHOLD,
    DEFAULT_MIN_IDENTICAL,
    DEFAULT_MIN_MODEL,
    METHOD_MEDIAN,
    METHOD_OLS,
    ols_fit,
    pearson,
    predict_durations,
    predicted_seconds,
)
from .recorder import RunRecorder, freeze_planned, runtime_step_id, step_identity
from .replay import (
    actual_ends,
    actual_intervals,
    executor_gated,
    expanded_step_ids,
    record_step_id,
    replay_timings,
)
from .report import (
    DEFAULT_CV_THRESHOLD,
    DEFAULT_MIN_RUNS,
    Report,
    RollUp,
    StepReport,
    build_report,
    duration_stats,
    filter_since,
    percentile,
)
from .store import (
    DEFAULT_RUNS_DIR,
    PROGRAMS_DIR_ENV,
    RUNS_DIR_ENV,
    find_program_for_record,
    find_run,
    list_runs,
    load_run,
    resolve_runs_dir,
    validate_run,
    write_run,
)
from .synth import synthesize_runs
from .usable import (
    is_usable_run,
    measured_steps,
    planned_duration,
    step_duration,
    step_reason,
    usable_steps,
)

__all__ = [
    "canonical_json",
    "load_program_for_hash",
    "program_version",
    "RunRecorder",
    "freeze_planned",
    "runtime_step_id",
    "step_identity",
    "actual_ends",
    "actual_intervals",
    "executor_gated",
    "expanded_step_ids",
    "record_step_id",
    "replay_timings",
    "DEFAULT_RUNS_DIR",
    "PROGRAMS_DIR_ENV",
    "RUNS_DIR_ENV",
    "find_program_for_record",
    "find_run",
    "list_runs",
    "load_run",
    "resolve_runs_dir",
    "validate_run",
    "write_run",
    "is_usable_run",
    "usable_steps",
    "measured_steps",
    "step_reason",
    "step_duration",
    "planned_duration",
    "build_report",
    "duration_stats",
    "percentile",
    "filter_since",
    "Report",
    "StepReport",
    "RollUp",
    "DEFAULT_MIN_RUNS",
    "DEFAULT_CV_THRESHOLD",
    "evaluate_program",
    "evaluate_all",
    "group_by_program",
    "split_chronologically",
    "sort_records",
    "program_stub_from_records",
    "Evaluation",
    "StepEvaluation",
    "GroupEvaluation",
    "DEFAULT_HOLDOUT",
    "DEFAULT_MIN_TOTAL_RUNS",
    "BASIS_ORDER",
    "synthesize_runs",
    "predict_durations",
    "predicted_seconds",
    "ols_fit",
    "pearson",
    "DEFAULT_MIN_IDENTICAL",
    "DEFAULT_MIN_MODEL",
    "DEFAULT_CORR_THRESHOLD",
    "BASIS_IDENTICAL",
    "BASIS_MODEL",
    "BASIS_NONE",
    "METHOD_OLS",
    "METHOD_MEDIAN",
    "declared_factors",
    "collect_factors",
    "coerce_factor_value",
    "parse_factor_args",
    "factors_from_env",
    "preset_answers",
    "prompt_for_factors",
    "propose_calibration",
    "apply_calibration",
    "calibration_effect",
    "current_values",
    "CalibrationProposal",
    "StepProposal",
]
