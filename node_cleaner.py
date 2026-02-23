#!/usr/bin/env python3
"""
node_cleaner.py — Interactive node_modules scanner & cleaner.

Usage:
    python3 node_cleaner.py [root_dir]

Key bindings:
    ↑/k, ↓/j  Navigate
    PgUp/PgDn  Scroll by page
    g/G        Jump to first/last
    D          Delete selected (with confirmation)
    Q/q/Esc    Quit
"""

import argparse
import curses
import os
import queue
import shutil
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class ScanState(Enum):
    SCANNING = auto()
    DONE = auto()


@dataclass
class NodeModulesEntry:
    abs_path: str
    rel_path: str
    size_kb: int
    size_human: str = ""
    status: str = "Active"
    deleted: bool = False
    marked: bool = False

    def __post_init__(self):
        if not self.size_human:
            self.size_human = format_size(self.size_kb)


# ---------------------------------------------------------------------------
# Size utilities
# ---------------------------------------------------------------------------

def format_size(size_kb: int) -> str:
    """Convert kilobytes to a human-readable string."""
    if size_kb == 0:
        return "0 B"
    size_b = size_kb * 1024
    for unit, threshold in [("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)]:
        if size_b >= threshold:
            return f"{size_b / threshold:.1f} {unit}"
    return f"{size_b} B"


def truncate_path(rel_path: str, max_width: int) -> str:
    """Truncate a path to fit within max_width, showing tail with '...' prefix."""
    if len(rel_path) <= max_width:
        return rel_path
    # Show as much of the tail as possible
    tail = rel_path[-(max_width - 3):]
    # Try to align to a path separator
    sep_idx = tail.find(os.sep)
    if sep_idx != -1 and sep_idx < 8:
        tail = tail[sep_idx:]
    return "..." + tail


def measure_size(abs_path: str) -> int:
    """Return disk usage of abs_path in KB using `du -sk`."""
    try:
        result = subprocess.run(
            ["du", "-sk", abs_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return int(result.stdout.split()[0])
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def find_node_modules(root: str, result_queue: queue.Queue, state: list):
    """
    Walk root, find node_modules dirs, measure sizes via thread pool,
    put NodeModulesEntry objects into result_queue.
    state[0] is set to ScanState.DONE when finished.
    """
    paths = []

    for dirpath, dirnames, _ in os.walk(root, topdown=True, onerror=lambda e: None):
        if "node_modules" in dirnames:
            paths.append(os.path.join(dirpath, "node_modules"))
            # Don't descend into node_modules
            dirnames.remove("node_modules")

    def measure_and_enqueue(abs_path: str):
        size_kb = measure_size(abs_path)
        # Show the project directory, not the node_modules dir itself
        parent_abs = os.path.dirname(abs_path)
        rel = os.path.relpath(parent_abs, root)
        entry = NodeModulesEntry(
            abs_path=abs_path,
            rel_path=rel,
            size_kb=size_kb,
        )
        result_queue.put(entry)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(measure_and_enqueue, p) for p in paths]
        for f in futures:
            f.result()  # propagate exceptions silently

    state[0] = ScanState.DONE
    result_queue.put(None)  # sentinel


def scanner_thread(root: str, result_queue: queue.Queue, state: list):
    t = threading.Thread(
        target=find_node_modules,
        args=(root, result_queue, state),
        daemon=True,
    )
    t.start()
    return t


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def _on_rm_error(func, path, exc_info):
    """onerror callback: chmod then retry."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        func(path)
    except Exception:
        pass


def delete_entry(entry: NodeModulesEntry) -> Optional[str]:
    """Delete an entry. Returns an error string on failure, None on success."""
    if not os.path.exists(entry.abs_path):
        return "Already gone"
    try:
        shutil.rmtree(entry.abs_path, onerror=_on_rm_error)
        return None
    except Exception as exc:
        return str(exc)


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

COL_STATUS_W = 12
COL_SIZE_W = 10
COL_SEP = 2  # gap between columns


COL_MARK_W = 2  # "● " or "  " prefix before path


def _path_col_width(width: int) -> int:
    return max(20, width - COL_MARK_W - COL_SIZE_W - COL_STATUS_W - COL_SEP * 2 - 2)


def draw_header(stdscr, root: str):
    h, w = stdscr.getmaxyx()
    title = "node_cleaner"
    root_str = f"Root: {root}"
    line = f"  {title}  {root_str}"
    try:
        stdscr.addstr(0, 0, line[:w], curses.A_BOLD)
    except curses.error:
        pass


def draw_column_headers(stdscr, width: int):
    path_w = _path_col_width(width)
    header = (
        f"{'':>{COL_MARK_W}}{'PATH':<{path_w}}"
        f"  {'SIZE':>{COL_SIZE_W}}"
        f"  {'STATUS':<{COL_STATUS_W}}"
    )
    try:
        stdscr.addstr(1, 0, header[:width], curses.A_UNDERLINE | curses.A_BOLD)
    except curses.error:
        pass


def draw_table(
    stdscr,
    entries: List[NodeModulesEntry],
    selected: int,
    scroll: int,
    height: int,
    width: int,
):
    """Draw the scrollable table body starting at row 2."""
    # Layout: row 0 header, row 1 col-headers, rows 2..H-4 table, rows H-3..H-1 footer
    table_rows = height - 5
    path_w = _path_col_width(width)

    for i in range(table_rows):
        idx = scroll + i
        row = 2 + i
        if idx >= len(entries):
            try:
                stdscr.move(row, 0)
                stdscr.clrtoeol()
            except curses.error:
                pass
            continue

        entry = entries[idx]
        marker = "● " if entry.marked else "  "
        path_str = truncate_path(entry.rel_path, path_w)
        line = (
            f"{marker}{path_str:<{path_w}}"
            f"  {entry.size_human:>{COL_SIZE_W}}"
            f"  {entry.status:<{COL_STATUS_W}}"
        )
        line = line[:width]

        attrs = curses.A_NORMAL
        if entry.deleted:
            attrs |= curses.A_DIM
        if entry.marked:
            attrs |= curses.A_BOLD
        if idx == selected:
            attrs |= curses.A_REVERSE

        try:
            stdscr.addstr(row, 0, line, attrs)
            stdscr.clrtoeol()
        except curses.error:
            pass


def draw_footer(stdscr, entries: List[NodeModulesEntry], scan_state: ScanState, height: int, width: int):
    active = [e for e in entries if not e.deleted]
    total_kb = sum(e.size_kb for e in active)
    total_str = format_size(total_kb)
    n_total = len(entries)
    n_active = len(active)
    n_marked = sum(1 for e in entries if e.marked and not e.deleted)

    scanning_indicator = "  [scanning...]" if scan_state == ScanState.SCANNING else ""
    marked_indicator = f"  [{n_marked} marked]" if n_marked else ""
    summary = f"  Total: {total_str}  ({n_total} folder{'s' if n_total != 1 else ''}, {n_active} active){marked_indicator}{scanning_indicator}"
    hint = "  [↑↓/jk] Navigate  [Space] Mark  [D] Delete marked (or current)  [Q/Esc] Quit"

    sep = "─" * width

    try:
        stdscr.addstr(height - 3, 0, sep[:width])
        stdscr.addstr(height - 2, 0, summary[:width], curses.A_BOLD)
        stdscr.clrtoeol()
        stdscr.addstr(height - 1, 0, hint[:width])
        stdscr.clrtoeol()
    except curses.error:
        pass


def draw_progress(stdscr, n: int, height: int, width: int):
    """Show a scanning progress notice when no entries yet."""
    if n == 0:
        msg = "  Scanning for node_modules..."
        try:
            stdscr.addstr(2, 0, msg[:width], curses.A_DIM)
        except curses.error:
            pass


def draw_confirm_dialog(stdscr, targets: List[NodeModulesEntry], height: int, width: int):
    """Draw a centered confirmation modal for one or many targets."""
    box_h = 7
    box_w = min(width - 4, 70)
    start_y = (height - box_h) // 2
    start_x = (width - box_w) // 2

    try:
        win = curses.newwin(box_h, box_w, start_y, start_x)
        win.erase()
        win.border()

        title = " Confirm Delete "
        win.addstr(0, (box_w - len(title)) // 2, title, curses.A_BOLD)

        inner = box_w - 4
        if len(targets) == 1:
            path_display = truncate_path(targets[0].rel_path, inner)
            win.addstr(2, 2, path_display[:inner])
            win.addstr(3, 2, f"Size: {targets[0].size_human}"[:inner])
        else:
            total_kb = sum(e.size_kb for e in targets)
            win.addstr(2, 2, f"{len(targets)} folders selected"[:inner])
            win.addstr(3, 2, f"Total: {format_size(total_kb)}"[:inner])

        confirm_line = "[Y] Delete   [Any other key] Cancel"
        win.addstr(5, (box_w - len(confirm_line)) // 2, confirm_line, curses.A_BOLD)

        win.refresh()
        return win
    except curses.error:
        return None


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

SCROLLOFF = 3  # lines of context to keep above/below cursor (vim-style)


def handle_input(key: int, selected: int, scroll: int, entries: List[NodeModulesEntry], height: int):
    """Handle navigation keys. Returns (new_selected, new_scroll, quit_flag, delete_flag, mark_flag)."""
    table_rows = height - 5
    n = len(entries)
    quit_flag = False
    delete_flag = False
    mark_flag = False

    if key in (curses.KEY_UP, ord("k")):
        selected = max(0, selected - 1)
    elif key in (curses.KEY_DOWN, ord("j")):
        selected = min(max(0, n - 1), selected + 1)
    elif key == curses.KEY_PPAGE:  # Page Up
        selected = max(0, selected - table_rows)
    elif key == curses.KEY_NPAGE:  # Page Down
        selected = min(max(0, n - 1), selected + table_rows)
    elif key == ord("g"):
        selected = 0
    elif key == ord("G"):
        selected = max(0, n - 1)
    elif key in (ord("Q"), ord("q"), 27):  # 27 = Esc
        quit_flag = True
    elif key == ord("D"):
        delete_flag = True
    elif key == ord(" "):
        mark_flag = True
    # lowercase 'd' intentionally ignored

    # Vim-like scrolloff: keep SCROLLOFF lines of context around cursor
    so = min(SCROLLOFF, table_rows // 2)
    if selected - scroll < so:
        scroll = max(0, selected - so)
    if selected >= scroll + table_rows - so:
        scroll = selected - table_rows + so + 1
    scroll = max(0, scroll)

    return selected, scroll, quit_flag, delete_flag, mark_flag


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main_loop(stdscr, root: str):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(100)

    try:
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        pass

    result_queue: queue.Queue = queue.Queue()
    scan_state = [ScanState.SCANNING]

    scanner_thread(root, result_queue, scan_state)

    entries: List[NodeModulesEntry] = []
    selected = 0
    scroll = 0

    while True:
        # Drain the queue
        try:
            while True:
                item = result_queue.get_nowait()
                if item is None:
                    pass  # sentinel already handled via scan_state
                else:
                    entries.append(item)
        except queue.Empty:
            pass

        h, w = stdscr.getmaxyx()
        stdscr.erase()

        draw_header(stdscr, root)
        draw_column_headers(stdscr, w)
        if not entries:
            draw_progress(stdscr, len(entries), h, w)
        else:
            # Clamp selected/scroll after possible resize
            n = len(entries)
            selected = min(selected, max(0, n - 1))
            table_rows = h - 5
            scroll = min(scroll, max(0, n - table_rows))
            draw_table(stdscr, entries, selected, scroll, h, w)

        draw_footer(stdscr, entries, scan_state[0], h, w)
        stdscr.refresh()

        # Input
        try:
            key = stdscr.getch()
        except curses.error:
            continue

        if key == -1:
            continue

        selected, scroll, quit_flag, delete_flag, mark_flag = handle_input(
            key, selected, scroll, entries, h
        )

        if quit_flag:
            break

        if mark_flag and entries:
            entry = entries[selected]
            if not entry.deleted:
                entry.marked = not entry.marked
            # Advance cursor down after marking (vim-style)
            if selected < len(entries) - 1:
                selected += 1
                table_rows = h - 5
                so = min(SCROLLOFF, table_rows // 2)
                if selected >= scroll + table_rows - so:
                    scroll = selected - table_rows + so + 1
                scroll = max(0, scroll)

        if delete_flag and entries:
            # Use all marked non-deleted entries, or fall back to current row
            targets = [e for e in entries if e.marked and not e.deleted]
            if not targets:
                cur = entries[selected]
                if not cur.deleted:
                    targets = [cur]
            if not targets:
                continue

            # Show confirmation dialog (blocking getch)
            stdscr.nodelay(False)
            stdscr.timeout(-1)
            draw_confirm_dialog(stdscr, targets, h, w)

            confirm_key = stdscr.getch()

            stdscr.nodelay(True)
            stdscr.timeout(100)

            if confirm_key in (ord("Y"), ord("y")):
                for entry in targets:
                    entry.status = "Deleting..."
                    entry.marked = False
                stdscr.erase()
                draw_header(stdscr, root)
                draw_column_headers(stdscr, w)
                draw_table(stdscr, entries, selected, scroll, h, w)
                draw_footer(stdscr, entries, scan_state[0], h, w)
                stdscr.refresh()

                for entry in targets:
                    error = delete_entry(entry)
                    if error:
                        entry.status = f"Err: {error[:8]}"
                    else:
                        entry.deleted = True
                        entry.status = "Deleted"
                        entry.size_kb = 0
                        entry.size_human = "—"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Interactive node_modules scanner & cleaner."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=os.getcwd(),
        help="Root directory to scan (default: current directory)",
    )
    args = parser.parse_args()
    root = os.path.abspath(os.path.expanduser(args.root))

    if not os.path.isdir(root):
        print(f"Error: '{root}' is not a directory.")
        raise SystemExit(1)

    curses.wrapper(main_loop, root)


if __name__ == "__main__":
    main()
