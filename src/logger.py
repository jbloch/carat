"""
Simple, thread-safe logging utility for CLI and GUI applications in the carat project.

This module is a singleton with global state. It is, however, thread-safe. It provides progress-line clearing for
smooth interleaving of background thread messages and real-time subprocess output in command line applications.
GUI applications can generate high-quality progress bars by providing appropriate callbacks.
"""

# Copyright (c) 2026 Joshua Bloch
# SPDX-License-Identifier: MIT

__author__ = "Joshua Bloch"
__copyright__ = "Copyright 2026, Joshua Bloch"
__license__ = "MIT"
__version__ = "1.0B"

import sys
import threading
from collections.abc import Callable

__all__ = ['init', 'emit']

# Global lock to prevent threads from garbling the console
_print_lock = threading.Lock()

# Whether we are currently in a sequence of progress messages. Used only if log_callback is none
_in_progress: bool = False

# Our log callback, which is called to emit log messages (typically to a GUI, but can be uses for many purposes).
# This variable is set by init_log_callback. If it has not been set, we log to stdout.
_log_callback: Callable[[str, bool], None] | None = None

def init(log_callback: Callable[[str, bool], None] | None) -> None:
    """
    Initializes the logger to use the specified callback. If this method is not called, or None is passed in,
    emit will log to stdout. The two arguments to log callback are the string to be logged, and whether it represents
    "progress," and should hence overwrite the previously logged string.
    """
    global _log_callback
    _log_callback = log_callback


def emit(line: str, is_progress: bool = False) -> None:
    """
    Emits the given line to the log_callback, if provided, or to stdout if it is not. If is_progress is true, then
    line represents progress, and should overwrite the previously logged string.
    """
    global _print_lock, _log_callback
    with _print_lock:
        if _log_callback:
            _log_callback(line, is_progress)
        else:
            global _in_progress
            if is_progress:
                # Adding \033[K ensures the rest of the previous line is erased
                sys.stdout.write(f"\r{line.strip()}\033[K")
                sys.stdout.flush()
                _in_progress = True
            else:
                if _in_progress:
                    print()  # Move to the next line so we don't overwrite the progress bar
                print(line.rstrip('\n'))
                _in_progress = False  # Reset state
