"""
Simple, thread-safe logging utility for CLI and GUI applications in the carat project.

Provides progress-line clearing for smooth interleaving of background thread messages and real-time subprocess output
in command line applications. GUI applications can generate high-quality progress bars by providing appropriate callbacks.
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

# Global lock to prevent threads from garbling the console
_print_lock = threading.Lock()

# Whether we are currently in a sequence of progress messages. Used only if log_callback is none
in_progress: bool = False

def emit(line: str, log_callback: Callable = None, is_progress: bool = False) -> None:
    """ Emits the given line to the log_callback, if provided, or to stdout if it is not. """
    with _print_lock:
        if log_callback:
            log_callback(line, is_progress)
        else:
            global in_progress
            if is_progress:
                # Adding \033[K ensures the rest of the previous line is erased
                sys.stdout.write(f"\r{line.strip()}\033[K")
                sys.stdout.flush()
                in_progress = True
            else:
                if in_progress:
                    print()  # Move to the next line so we don't overwrite the progress bar
                print(line.rstrip('\n'))
                in_progress = False  # Reset state

