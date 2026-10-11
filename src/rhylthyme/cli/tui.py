"""Terminal (curses) front end for the program runner.

The engine lives in :mod:`rhylthyme.program_runner`; this module draws it,
reads keys, and wires a run to predictions, recording and the bridge.
"""

import argparse
import curses
import json
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from rhylthyme.instruments import (
    InstrumentSetupError,
    open_instruments,
    program_uses_instruments,
)
from rhylthyme.program_runner import ProgramRunner, SortMode, StepStatus
from rhylthyme.validate_program import (
    load_program_file,
    perform_additional_validations,
    validate_program,
)

from .bridge import start_bridge


def draw_ui(stdscr, runner: ProgramRunner) -> None:
    """Draw the user interface."""
    stdscr.clear()
    height, width = stdscr.getmaxyx()

    # Initialize colors
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)  # Running
    curses.init_pair(2, curses.COLOR_BLUE, -1)  # Completed
    curses.init_pair(3, curses.COLOR_YELLOW, -1)  # Waiting
    curses.init_pair(4, curses.COLOR_RED, -1)  # Aborted
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)  # Upcoming events

    # Helper function to safely add strings to the screen
    def safe_addstr(y, x, text, attr=curses.A_NORMAL):
        # Ensure we don't write beyond the screen width
        if x >= width:
            return
        # Truncate the string if it would go beyond the screen width
        max_len = width - x - 1
        if max_len <= 0:
            return
        text = str(text)[:max_len]
        try:
            stdscr.addstr(y, x, text, attr)
        except curses.error:
            # Catch any curses errors (like writing to the bottom-right corner)
            pass

    # Draw header
    program_name = runner.program.get("name", "Unnamed Program")
    header = f" {program_name} "
    safe_addstr(0, (width - len(header)) // 2, header, curses.A_BOLD)

    # Draw status
    if runner.is_paused:
        state = "PAUSED"
    elif runner.is_running:
        state = "Running"
    else:
        state = "Stopped"
    status = f" Status: {state} | Time Scale: {runner.time_scale}x | Actors: {runner.actors_available} "
    safe_addstr(1, (width - len(status)) // 2, status)

    # Draw actor usage
    actor_percentage = (
        (runner.actor_usage / runner.actors_available * 100)
        if runner.actors_available > 0
        else 0
    )
    actor_str = f" Actor Usage: {runner.actor_usage:.1f}/{runner.actors_available} ({actor_percentage:.0f}%) "
    safe_addstr(
        2,
        (width - len(actor_str)) // 2,
        actor_str,
        curses.color_pair(1) if actor_percentage > 80 else curses.A_NORMAL,
    )

    # Draw sort mode
    sort_mode_text = f" Sort: {runner.sort_mode.value.capitalize()} "
    safe_addstr(1, max(0, width - len(sort_mode_text) - 2), sort_mode_text)

    # Draw time
    if runner.program_start_time:
        elapsed = runner.current_time - runner.program_start_time
        time_str = f" Elapsed: {runner.format_time(elapsed)} "
        safe_addstr(3, (width - len(time_str)) // 2, time_str)

    # Draw steps table
    steps_info = runner.get_all_steps_display_info()

    # Table header
    header_y = 5
    safe_addstr(header_y, 2, "Step ID", curses.A_BOLD)
    safe_addstr(header_y, 20, "Name", curses.A_BOLD)
    safe_addstr(header_y, 40, "Track", curses.A_BOLD)

    # Add sort indicator to the status header if sorting by status
    if runner.sort_mode == SortMode.STATUS:
        safe_addstr(header_y, 55, "Status ↑", curses.A_BOLD)
    else:
        safe_addstr(header_y, 55, "Status", curses.A_BOLD)

    safe_addstr(header_y, 65, "Progress", curses.A_BOLD)

    # Add sort indicator to the remaining header if sorting by remaining time
    if runner.sort_mode == SortMode.REMAINING:
        safe_addstr(header_y, 80, "Remaining ↑", curses.A_BOLD)
    else:
        safe_addstr(header_y, 80, "Remaining", curses.A_BOLD)

    # Table rows
    last_row_y = header_y  # Track the last row we drew
    for i, step_info in enumerate(steps_info):
        row_y = header_y + 1 + i
        if (
            row_y >= height - 13
        ):  # Leave space for resources, upcoming events, and triggers
            break
        last_row_y = row_y

        # Get status color
        status_color = 0
        if step_info["status"] == "RUNNING":
            status_color = 1
        elif step_info["status"] == "COMPLETED":
            status_color = 2
        elif step_info["status"] == "WAITING_FOR_MANUAL":
            status_color = 3
        elif step_info["status"] == "ABORTED":
            status_color = 4

        # Get status display
        status_display = runner.get_status_display(step_info["status"])

        # Highlight running steps
        attr = curses.A_NORMAL
        if step_info["status"] == "RUNNING":
            attr = curses.A_BOLD
        elif step_info["status"] == "FAILED":
            attr = curses.color_pair(4) | curses.A_BOLD

        # Draw selection indicator
        if step_info.get("selected", False):
            safe_addstr(row_y, 0, ">", curses.A_BOLD)

        # Instance groups: one row per replicated step, expandable with 'g'.
        is_group = step_info.get("row_type") == "group"
        if is_group:
            attr = attr | curses.A_BOLD
            marker = "\u25be " if step_info.get("expanded") else "\u25b8 "
            name_cell = marker + step_info["name"]
        else:
            indent = "  " * step_info.get("depth", 0)
            name_cell = indent + step_info["name"]

        safe_addstr(row_y, 2, step_info.get("id_display", step_info["id"])[:17], attr)
        safe_addstr(row_y, 20, name_cell[:19], attr)
        safe_addstr(
            row_y, 40, step_info.get("track_display", step_info["track"])[:13], attr
        )
        safe_addstr(row_y, 55, status_display[:8], attr)

        # Instrument steps: a tool badge instead of a progress bar (only the
        # instrument knows how far along it is)
        tool = step_info.get("instrument_tool")
        if tool and not is_group:
            safe_addstr(row_y, 65, f"[{tool}]"[:14], attr)
        # Draw progress bar for running steps
        elif step_info["status"] == "RUNNING" and step_info["progress"] >= 0:
            progress_width = 10
            filled = int((step_info["progress"] / 100) * progress_width)
            # Show at least 1 symbol if progress > 0 but < 10%
            if step_info["progress"] > 0 and filled == 0:
                progress_bar = "[>" + " " * (progress_width - 1) + "]"
            elif filled == progress_width:
                progress_bar = "[" + "#" * filled + "]"
            else:
                progress_bar = (
                    "[" + "#" * filled + ">" + " " * (progress_width - filled - 1) + "]"
                )
            safe_addstr(row_y, 65, progress_bar, attr)
        else:
            safe_addstr(row_y, 65, "N/A", attr)

        if is_group:
            summary = step_info.get("summary", "")
            safe_addstr(row_y, 80, summary[: max(0, width - 82)], attr)
        elif step_info.get("waiting_on"):
            safe_addstr(row_y, 80, f"waiting on {step_info['waiting_on']}", attr)
        elif step_info.get("failure_code"):
            safe_addstr(row_y, 80, str(step_info["failure_code"]), attr)
        else:
            safe_addstr(row_y, 80, step_info["remaining"], attr)

    # Draw resource usage (left column in bottom section)
    resource_y = height - 12  # Move up to make room for actor types
    safe_addstr(resource_y, 2, "Resource Usage:", curses.A_BOLD)

    resource_info = runner.get_resource_usage_display()
    for i, resource in enumerate(resource_info):
        row_y = resource_y + 1 + i
        if row_y >= height - 8:  # Leave space for actor types, triggers and status
            break

        safe_addstr(row_y, 4, f"{resource['task_type']}: {resource['usage']}")

        # Draw usage bar
        bar_width = 20
        filled = int((resource["percentage"] / 100) * bar_width)
        bar = "[" + "#" * filled + " " * (bar_width - filled) + "]"
        safe_addstr(row_y, 25, bar)

    # Draw actor types usage
    actor_types_y = height - 8
    safe_addstr(actor_types_y, 2, "Actor Types:", curses.A_BOLD)

    actor_types_info = runner.get_actor_types_display()
    for i, actor_type in enumerate(actor_types_info):
        row_y = actor_types_y + 1 + i
        if row_y >= height - 4:  # Leave space for triggers and status
            break

        safe_addstr(row_y, 4, f"{actor_type['actor_type']}: {actor_type['usage']}")

        # Draw usage bar
        bar_width = 20
        filled = int((actor_type["percentage"] / 100) * bar_width)
        bar = "[" + "#" * filled + " " * (bar_width - filled) + "]"
        color_attr = (
            curses.color_pair(1) if actor_type["percentage"] > 80 else curses.A_NORMAL
        )
        safe_addstr(row_y, 25, bar, color_attr)

    # Draw upcoming events (right column in bottom section)
    events_col_x = 55  # Start column for events (right of resource usage)
    events_y = resource_y  # Same Y position as resource usage

    safe_addstr(
        events_y, events_col_x, "Upcoming Events:", curses.A_BOLD | curses.color_pair(5)
    )

    # Get upcoming events
    upcoming_events = runner.get_upcoming_events(8)  # Show up to 8 events

    if upcoming_events:
        # Draw event header
        safe_addstr(
            events_y + 1,
            events_col_x + 2,
            "Event",
            curses.A_BOLD | curses.color_pair(5),
        )
        safe_addstr(
            events_y + 1,
            events_col_x + 10,
            "Step",
            curses.A_BOLD | curses.color_pair(5),
        )
        safe_addstr(
            events_y + 1, events_col_x + 30, "In", curses.A_BOLD | curses.color_pair(5)
        )

        # Draw events
        for i, event in enumerate(upcoming_events):
            row_y = events_y + 2 + i
            if row_y >= height - 4:  # Stop before triggers section
                break

            event_type = event["event_type"].capitalize()
            step_name = event["name"]
            time_str = event["time_str"]

            # Ensure we don't exceed terminal width
            if events_col_x + 35 < width:
                safe_addstr(
                    row_y, events_col_x + 2, event_type[:5], curses.color_pair(5)
                )
                safe_addstr(
                    row_y, events_col_x + 10, step_name[:18], curses.color_pair(5)
                )
                safe_addstr(
                    row_y, events_col_x + 30, time_str[:10], curses.color_pair(5)
                )
    else:
        safe_addstr(
            events_y + 1, events_col_x + 2, "No upcoming events", curses.color_pair(5)
        )

    # Draw available triggers (while a failed step holds the program, its
    # banner takes this place instead)
    triggers_y = height - 4
    available_triggers = [] if runner.failed_steps else runner.get_available_triggers()
    if available_triggers:
        safe_addstr(triggers_y, 2, "Available Triggers:", curses.A_BOLD)
        trigger_names = [trigger["name"] for trigger in available_triggers]
        triggers_str = ", ".join(trigger_names)
        safe_addstr(triggers_y + 1, 4, triggers_str[: width - 8])

    # Failed instrument steps: what failed and what the operator can do
    if runner.failed_steps:
        failed = runner.steps[runner.failed_steps[0]]
        more = len(runner.failed_steps) - 1
        banner = f" FAILED {runner.failure_text(failed)}" + (
            f" (+{more} more)" if more else ""
        )
        banner += " | r: retry  x: skip (mark done)  A: abort program "
        safe_addstr(
            height - 4, 2, banner[: width - 4], curses.A_BOLD | curses.A_REVERSE
        )

    # Draw status message
    status_y = height - 2
    if runner.status_message:
        safe_addstr(status_y, 2, runner.status_message[: width - 4])

    # Draw help
    help_text = " q: Quit | s: Start | p: Pause | ↑↓: Select | g: Group | t: Trigger | a: Abort | c: Complete | T: Menu | +/-: Speed | o: Sort "
    # Ensure help text fits on screen
    if len(help_text) > width - 2:
        help_text = help_text[: width - 5] + "..."
    safe_addstr(height - 1, max(0, (width - len(help_text)) // 2), help_text)

    stdscr.refresh()


def handle_input(stdscr, runner: ProgramRunner) -> bool:
    """Handle user input."""
    try:
        key = stdscr.getkey()
    except:
        return True

    if key == "q":
        return False
    elif key == "s" and not runner.is_running:
        runner.command_queue.put("start_program")
    elif key == "p":
        runner.toggle_pause()
    elif key in ("r", "x") and runner.failed_steps:
        # Retry / skip the selected failed step, else the first failure
        selected = runner.get_selected_step_id()
        target = selected if selected in runner.failed_steps else ""
        verb = "retry" if key == "r" else "skip"
        runner.command_queue.put(f"{verb}:{target}")
    elif key == "A":
        # Abort the whole program; press twice to confirm
        now = time.time()
        if runner._abort_armed_at and now - runner._abort_armed_at < 3:
            runner._abort_armed_at = None
            runner.command_queue.put("abort_program:Aborted by operator")
        else:
            runner._abort_armed_at = now
            runner.status_message = "Press A again within 3 s to abort the program."
    elif key == "KEY_UP" or key == "k":
        # Select previous step
        runner.select_previous_step()
    elif key == "KEY_DOWN" or key == "j":
        # Select next step
        runner.select_next_step()
    elif key == "g" or key == "\n" or key == "KEY_ENTER":
        # Expand or collapse the selected replicate-instance group
        runner.toggle_group()
        return True
    elif key == "t":
        # Get the selected step
        selected_row = runner.get_selected_row()
        if selected_row and selected_row.get("row_type") == "group":
            runner.toggle_group(selected_row.get("instance_of"))
            runner.status_message += " Select an instance to trigger it."
            return True
        selected_step_id = runner.get_selected_step_id()
        if not selected_step_id or selected_step_id not in runner.steps:
            runner.status_message = "No step selected."
            return True

        selected_step = runner.steps[selected_step_id]

        # Check if the step has a manual trigger or can be aborted
        # Check both duration triggerName and start trigger triggerName
        trigger_name = selected_step.manual_trigger_name or getattr(
            selected_step, "manual_start_trigger_name", None
        )
        if not trigger_name:
            if selected_step.can_be_aborted():
                runner.status_message = f"Step '{selected_step.name}' has no manual trigger. Press 'a' to abort or 'T' for menu."
            else:
                runner.status_message = (
                    f"Step '{selected_step.name}' has no manual trigger."
                )
            return True

        # Check if the step can be triggered to START
        if (
            selected_step.status == StepStatus.PENDING
            and selected_step.has_manual_trigger()
        ):
            # Use the start trigger name if available, otherwise fall back to duration trigger
            start_name = (
                getattr(selected_step, "manual_start_trigger_name", None)
                or selected_step.manual_trigger_name
            )
            runner.command_queue.put(f"trigger:{start_name}:{selected_step_id}")
        elif selected_step.status == StepStatus.RUNNING and (
            selected_step.duration_type == "variable"
            or selected_step.duration_type == "indefinite"
        ):
            # Check if we've reached minimum duration for variable steps
            if selected_step.duration_type == "variable":
                progress = selected_step.get_progress(runner.current_time)
                min_progress = (
                    selected_step.min_seconds / selected_step.default_seconds
                ) * 100
                if progress < min_progress:
                    runner.status_message = f"Step '{selected_step.name}' hasn't reached minimum duration yet."
                    return True

            # Trigger the step to COMPLETE (use duration trigger name)
            runner.command_queue.put(
                f"trigger:{selected_step.manual_trigger_name}:{selected_step_id}"
            )
        else:
            runner.status_message = (
                f"Step '{selected_step.name}' cannot be triggered in its current state."
            )
            return True
    elif key == "a":
        # Abort the selected step
        selected_step_id = runner.get_selected_step_id()
        if not selected_step_id or selected_step_id not in runner.steps:
            runner.status_message = "No step selected."
            return True

        selected_step = runner.steps[selected_step_id]

        # Check if the step can be aborted
        if selected_step.can_be_aborted():
            runner.command_queue.put(f"abort:{selected_step_id}")
            runner.status_message = f"Aborting step '{selected_step.name}'..."
        else:
            runner.status_message = (
                f"Step '{selected_step.name}' cannot be aborted (not running)."
            )
        return True
    elif key == "c":
        # Force complete the selected running step (debug feature)
        selected_step_id = runner.get_selected_step_id()
        if not selected_step_id or selected_step_id not in runner.steps:
            runner.status_message = "No step selected."
            return True

        selected_step = runner.steps[selected_step_id]

        if selected_step.status == StepStatus.RUNNING:
            runner.complete_step(selected_step, runner.current_time)
            runner.status_message = f"Force completed step '{selected_step.name}'"
        else:
            runner.status_message = f"Step '{selected_step.name}' is not running (status: {selected_step.status.value})"
        return True
    elif key == "T":
        # Show trigger selection menu (original behavior)
        available_triggers = runner.get_available_triggers()
        if not available_triggers:
            runner.status_message = "No triggers available."
            return True

        # Create a submenu for trigger selection
        height, width = stdscr.getmaxyx()
        menu_height = min(len(available_triggers) + 4, height - 4)
        menu_width = min(60, width - 4)  # Wider menu to accommodate more information
        menu_y = (height - menu_height) // 2
        menu_x = (width - menu_width) // 2

        menu_win = curses.newwin(menu_height, menu_width, menu_y, menu_x)
        menu_win.box()
        menu_win.addstr(1, 2, "Select a trigger:", curses.A_BOLD)

        for i, trigger in enumerate(available_triggers):
            if i < menu_height - 4:
                if trigger["type"] == "program":
                    menu_win.addstr(i + 2, 2, f"{i+1}. {trigger['name']}")
                else:
                    # Show more details for step triggers
                    menu_win.addstr(
                        i + 2,
                        2,
                        f"{i+1}. {trigger['name']} (Track: {trigger['track_id']})",
                    )

        menu_win.addstr(menu_height - 1, 2, "Enter number or ESC to cancel")
        menu_win.refresh()

        # Get user selection
        curses.echo()
        selection = ""
        while True:
            try:
                ch = menu_win.getkey()
                if ch == "\x1b":  # ESC
                    break
                elif ch == "\n":  # Enter
                    if selection and selection.isdigit():
                        idx = int(selection) - 1
                        if 0 <= idx < len(available_triggers):
                            trigger = available_triggers[idx]
                            if trigger["id"] == "start_program":
                                runner.command_queue.put("start_program")
                            else:
                                trigger_type, trigger_name, step_id = trigger[
                                    "id"
                                ].split(":", 2)
                                runner.command_queue.put(
                                    f"trigger:{trigger_name}:{step_id}"
                                )
                    break
                elif ch.isdigit():
                    selection += ch
            except:
                break

        curses.noecho()
        stdscr.clear()
    elif key == "+" or key == "=":
        runner.time_scale = min(100.0, runner.time_scale * 2)
    elif key == "-" or key == "_":
        runner.time_scale = max(0.1, runner.time_scale / 2)
    elif key == "o" or key == "O":
        # Toggle sort mode
        if runner.sort_mode == SortMode.DEFAULT:
            runner.sort_mode = SortMode.REMAINING
            runner.status_message = "Sorted by remaining time"
        elif runner.sort_mode == SortMode.REMAINING:
            runner.sort_mode = SortMode.STATUS
            runner.status_message = "Sorted by status"
        else:
            runner.sort_mode = SortMode.DEFAULT
            runner.status_message = "Default sort order"

    return True


def run_finished(runner: ProgramRunner) -> bool:
    """True once a started run has completed or been aborted."""
    return bool(
        runner.program_started
        and runner.program_start_time is not None
        and not runner.is_running
    )


def main_loop(stdscr, runner: ProgramRunner, exit_when_done: bool = False) -> None:
    """Main loop for the program runner.

    With exit_when_done, return a few seconds after the run has completed or
    been aborted instead of waiting for 'q' (a bridge then waits for the next
    run from the web).
    """
    # Set up curses
    curses.curs_set(0)  # Hide cursor
    stdscr.timeout(100)  # Set non-blocking input timeout

    # Start the program
    runner.start()

    # Main loop
    running = True
    finished_at: Optional[float] = None
    while running:
        # Update program state
        runner.update()

        # Draw UI
        draw_ui(stdscr, runner)

        # Handle input
        running = handle_input(stdscr, runner)

        if exit_when_done and run_finished(runner):
            if finished_at is None:
                finished_at = time.time()
            elif time.time() - finished_at > 3:
                running = False

        # Sleep to limit CPU usage
        time.sleep(0.05)


def _load_history_records(
    history_file: Optional[str],
    runs_dir: Optional[str],
    program_id: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Run records to predict from: one file if given, else the runs directory.

    A ``--history`` file may hold a single record, a bare list of records, or
    an object with a ``runs`` array (the shape of the synthetic corpora in
    ``tests/fixtures/history``), so a corpus can be pointed at directly.
    """
    if history_file:
        try:
            with open(history_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"Could not read history from {history_file}: {exc}")
            return []
        if isinstance(payload, dict) and isinstance(payload.get("runs"), list):
            return [r for r in payload["runs"] if isinstance(r, dict)]
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict) and payload.get("steps") is not None:
            return [payload]
        return []
    from rhylthyme.history.store import list_runs

    return list_runs(runs_dir, program_id)


def _wants_predicted_offsets(program: Dict[str, Any]) -> bool:
    """Does ``program`` ask for predicted negative offsets (metadata.offsetsUse)?"""
    metadata = program.get("metadata")
    return isinstance(metadata, dict) and metadata.get("offsetsUse") == "predicted"


def _parse_key_values(pairs: Optional[Sequence[str]]) -> Dict[str, Any]:
    """``["turkeyKg=7", "oven=gas"]`` -> ``{"turkeyKg": "7", "oven": "gas"}``."""
    out: Dict[str, Any] = {}
    for pair in pairs or []:
        text = str(pair)
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def prepare_predictions(
    program: Dict[str, Any],
    source_program: Optional[Dict[str, Any]] = None,
    *,
    runs_dir: Optional[str] = None,
    history_file: Optional[str] = None,
    use_history: bool = True,
    user_tags: Optional[Dict[str, Any]] = None,
    predict_context: Optional[Sequence[str]] = None,
    echo_fn=print,
) -> Dict[str, Dict[str, Any]]:
    """
    Predict this program's step durations from run history, for the offsets.

    Returns ``{}`` unless the program asks for it with
    ``metadata.offsetsUse: "predicted"`` and history exists. The context
    predicted for is the answers to the declared variance factors (the same
    ``--factor`` answers the record stores in ``context.userTags``),
    overridden by ``--predict-context KEY=VALUE``.

    Executor-controlled steps are excluded by passing the inferentiality
    verdicts, so a step whose length the person decides gets no prediction at
    all rather than a confident median of other people's choices.
    """
    if not _wants_predicted_offsets(program):
        return {}
    if not use_history:
        echo_fn(
            "offsetsUse is 'predicted' but --no-history was given: "
            "negative offsets will use the planned durations."
        )
        return {}

    from rhylthyme.history.hash import program_version
    from rhylthyme.history.predict import predict_durations
    from rhylthyme.history.report import build_report

    program_id = program.get("programId")
    records = _load_history_records(history_file, runs_dir, program_id)
    if not records:
        echo_fn(
            "offsetsUse is 'predicted' but no runs are recorded for "
            f"'{program_id}': negative offsets will use the planned durations."
        )
        return {}

    context = dict(user_tags or {})
    context.update(_parse_key_values(predict_context))
    hashed = source_program if source_program is not None else program
    report = build_report(records, program)
    predictions = predict_durations(
        program,
        records,
        environment_id=program.get("environment"),
        user_tags=context,
        program_version=program_version(hashed),
        verdicts=report,
    )
    usable = {
        step_id: p
        for step_id, p in predictions.items()
        if p.get("basis") not in (None, "none") and p.get("seconds") is not None
    }
    echo_fn(
        f"Predicted durations from {len(records)} recorded run(s): "
        f"{len(usable)} of {len(predictions)} steps have a prediction "
        "(used only for negative offsets on indefinite steps)."
    )
    return predictions


def _confirm_live_run(pre_confirmed: bool) -> bool:
    """Ask before a --live run moves real hardware."""
    if pre_confirmed:
        return True
    if not sys.stdin.isatty():
        print("--live needs confirmation: run it in a terminal or pass --confirm-live.")
        return False
    try:
        answer = input("Type 'live' to send these commands to real instruments: ")
    except EOFError:
        return False
    return answer.strip().lower() == "live"


def run_program(
    program_file: str,
    schema_file: str = "program_schema.json",
    time_scale: float = 1.0,
    validate: bool = True,
    auto_start: bool = False,
    environment: Optional[str] = None,
    record: bool = True,
    runs_dir: Optional[str] = None,
    factors: Optional[Sequence[str]] = None,
    factor_prompt: bool = True,
    history_file: Optional[str] = None,
    use_history: bool = True,
    predict_context: Optional[Sequence[str]] = None,
    workcell: Optional[str] = None,
    live: bool = False,
    confirm_live: bool = False,
    bridge: bool = False,
    program_data: Optional[Dict[str, Any]] = None,
    program_id: Optional[str] = None,
    exit_when_done: bool = False,
    prepared_instruments: Optional[Any] = None,
    bridge_allows_live: bool = False,
) -> Optional[str]:
    """
    Run a program file with the interactive UI.

    Args:
        program_file: Path to the program file (JSON or YAML)
        schema_file: Path to the schema file (JSON or YAML)
        time_scale: Time scale factor
        validate: Whether to validate the program before running
        auto_start: Whether to automatically start the program
        environment: Environment ID to use (overrides program environment setting)
        record: Write a run record (planned vs actual) when the run ends
        runs_dir: Directory for run records (default: $RHYLTHYME_RUNS_DIR or
            ~/.rhylthyme/runs)
        factors: Pre-supplied answers to the program's declared variance
            factors, as ``key=value`` strings (``--factor``); they override
            ``RHYLTHYME_FACTORS`` and suppress the prompt for those keys
        factor_prompt: Ask on the plain terminal, before the TUI starts, for
            any declared variance factor that has no pre-supplied answer
        history_file: Run records to predict durations from instead of the
            runs directory (``--history``)
        use_history: Load history at all (``--no-history`` turns it off);
            only matters for a program with ``metadata.offsetsUse:
            "predicted"``
        predict_context: ``key=value`` context to predict for
            (``--predict-context``), defaulting to the factor answers
        workcell: Workcell file mapping the program's instrument tools to
            galago-tools or LabMCP servers (``--workcell``); required when any
            step has an ``instrument``. Tools run simulated unless
            ``live``.
        live: Run instrument steps on real hardware (``--live``). Shows what
            will run and asks for confirmation before any tool is configured.
        confirm_live: Skip that question (``--confirm-live``); required when
            stdin is not a terminal.
        bridge: Also show the run live on rhylthyme.com (``rhylthyme
            bridge``); needs ``rhylthyme login`` and a workcell.
        program_data: The program itself, instead of reading program_file
            (a run started from the web; program_file is then only a label).
        program_id: The saved program's id, shown on the Bridges page.
        exit_when_done: Leave the terminal UI on its own once the run has
            completed or been aborted (so a bridge can go back to waiting).
        prepared_instruments: An InstrumentSession already opened and
            prepared for this program (a bridge configures the tools before it
            accepts a run from the web); used as is instead of opening one.

    Returns:
        Path of the written run record, or None if none was written.
    """
    # Load the program
    if program_data is not None:
        program = json.loads(json.dumps(program_data))
    else:
        program = load_program_file(program_file)

    # Keep the program exactly as authored, for the run record's
    # programVersion and for the identical-context prediction lookup.
    source_program = None
    if program_data is not None:
        source_program = json.loads(json.dumps(program_data))
    elif record or _wants_predicted_offsets(program):
        try:
            from rhylthyme.history.hash import load_program_for_hash

            source_program = load_program_for_hash(program_file)
        except Exception:
            source_program = json.loads(json.dumps(program))

    # Handle environment resolution using the CLI's environment loader
    try:
        from rhylthyme.cli.main import get_environment_loader

        loader = get_environment_loader()
    except ImportError:
        # Fallback to default loader if CLI module not available
        from rhylthyme.environment_loader import EnvironmentLoader

        loader = EnvironmentLoader()

    if environment:
        # Override environment if specified via command line
        program["environment"] = environment
    elif "environmentType" in program and "environment" not in program:
        # Try to resolve environment type to a specific environment
        environment_type = program["environmentType"]
        default_env = loader.get_default_environment_for_type(environment_type)
        if default_env:
            program["environment"] = default_env
            print(
                f"Using default environment '{default_env}' for type '{environment_type}'"
            )
        else:
            available_envs = loader.list_environments_by_type(environment_type)
            if available_envs:
                print(f"Available environments for type '{environment_type}':")
                for env in available_envs:
                    print(f"  - {env['id']}: {env['name']}")
                print(f"\nUse -e/--environment to specify which environment to use.")
                print(f"Running without environment (unlimited resources)...")
            else:
                print(f"No environments found for type '{environment_type}'")
                print(f"Running without environment (unlimited resources)...")
            # Don't exit - allow program to run without environment constraints

    # Validate if requested
    if validate:
        try:
            schema = load_program_file(schema_file)
            is_valid, schema_errors = validate_program(program, schema)
            additional_errors = perform_additional_validations(
                program, workcell=workcell
            )

            if not is_valid or additional_errors:
                print(f"Program validation failed for {program_file}:")

                if schema_errors:
                    print("\nSchema validation errors:")
                    for error in schema_errors:
                        print(f"  - {error}")

                if additional_errors:
                    print("\nAdditional validation errors:")
                    for error in additional_errors:
                        print(f"  - {error}")

                sys.exit(1)

            print(f"Program {program_file} is valid.")
        except Exception as e:
            print(f"Error validating program: {e}")
            sys.exit(1)

    # Pre-flight: ask for the program's declared variance factors on the plain
    # terminal, before curses takes the screen. Skipped entirely when the
    # program declares none, or when recording is off.
    predicted_offsets = _wants_predicted_offsets(program)
    user_tags = {}
    if record or predicted_offsets:
        from rhylthyme.history.factors import collect_factors

        user_tags = collect_factors(program, factor_args=factors, prompt=factor_prompt)

    # Create the program runner
    runner = ProgramRunner(program, time_scale=time_scale, auto_start=auto_start)

    # Instrument steps: configure the workcell's tools before the clock starts
    instruments = None
    publisher = None
    if workcell or program_uses_instruments(program):
        if not workcell:
            print(
                "This program has instrument steps; pass --workcell FILE "
                "naming the tools to run them on."
            )
            sys.exit(1)
        try:
            if prepared_instruments is not None:
                instruments = prepared_instruments
            else:
                instruments = open_instruments(runner.program, workcell, live=live)
                if live:
                    print("\n".join(instruments.summary(runner.program)))
                    problems = instruments.preflight_problems()
                    if problems:
                        instruments.shutdown()
                        print(
                            "Live run refused: every instrument must be ready "
                            "first. Nothing was sent to the instruments.\n  "
                            + "\n  ".join(problems)
                        )
                        sys.exit(1)
                    if not _confirm_live_run(confirm_live):
                        instruments.shutdown()
                        print(
                            "Live run cancelled; nothing was sent to the instruments."
                        )
                        sys.exit(1)
                instruments.prepare()
        except InstrumentSetupError as e:
            if instruments is not None:
                instruments.shutdown()
            print(f"Cannot start instruments:\n{e}")
            sys.exit(1)
        print("\n".join(instruments.report()))
        instruments.attach(runner)
        if bridge:
            try:
                publisher = start_bridge(
                    runner,
                    instruments,
                    runner.program,
                    program_id=program_id,
                    allows_live=bridge_allows_live,
                )
            except InstrumentSetupError as e:
                instruments.shutdown()
                print(f"Cannot start the bridge:\n{e}")
                sys.exit(1)
            print("Live on rhylthyme.com: open Bridges on the lab site.")
    elif live:
        print("--live has no effect: this program has no instrument steps.")

    # Predicted offsets: history is read once, before the clock starts, and the
    # predictions are frozen for the whole run.
    if predicted_offsets:
        runner.set_predictions(
            prepare_predictions(
                program,
                source_program,
                runs_dir=runs_dir,
                history_file=history_file,
                use_history=use_history,
                user_tags=user_tags,
                predict_context=predict_context,
            )
        )

    # Attach the run recorder (writes planned vs actual on exit)
    recorder = None
    if record:
        from rhylthyme.history.recorder import RunRecorder

        recorder = RunRecorder(
            runner, source_program=source_program, runs_dir=runs_dir
        ).attach()
        if user_tags:
            recorder.context["userTags"].update(user_tags)

    # Run the program with curses UI; the record is written however we exit
    outcome = None
    written = None
    try:
        # Tests reach the runner through this function's only closure cell,
        # so exit_when_done rides in a default argument rather than a closure.
        def _ui(stdscr, _exit: bool = exit_when_done) -> None:
            main_loop(stdscr, runner, exit_when_done=_exit)

        curses.wrapper(_ui)
    except KeyboardInterrupt:
        print("Program execution interrupted.")
        outcome = "abandoned"
    finally:
        if publisher is not None:
            publisher.stop()
        if instruments is not None:
            # Stop what an unfinished run left running, and record the stops
            instruments.finish(runner)
            pending = instruments.shutdown()
            if pending:
                print(
                    "Instrument commands still running when the runner stopped: "
                    + ", ".join(pending)
                )
        if recorder is not None:
            written = recorder.finalize(outcome=outcome)
            if written:
                print(f"Run record written to {written}")
    return str(written) if written else None


def main():
    parser = argparse.ArgumentParser(description="Run a real-time program file")
    parser.add_argument("program_file", help="Program JSON file to run")
    parser.add_argument(
        "--schema",
        default="program_schema.json",
        help="Path to the schema file (default: program_schema.json)",
    )
    parser.add_argument(
        "--time-scale", type=float, default=1.0, help="Time scale factor (default: 1.0)"
    )
    parser.add_argument(
        "--validate", action="store_true", help="Validate the program before running"
    )
    parser.add_argument(
        "--auto-start", action="store_true", help="Automatically start the program"
    )

    args = parser.parse_args()

    run_program(
        args.program_file, args.schema, args.time_scale, args.validate, args.auto_start
    )


if __name__ == "__main__":
    main()
