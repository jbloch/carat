"""
A thread-safe logging utility for CLI and GUI applications.
Provides progress-line clearing for smooth interleaving of background 
thread messages and real-time subprocess output.
"""

import sys
import threading

# Global lock to prevent threads from garbling the console
_print_lock = threading.Lock()

def emit(line, log_callback=None, is_progress=False):
    """Thread-safe emission with progress-line clearing."""
    with _print_lock:
        if log_callback:
            log_callback(line)
        else:
            if is_progress:
                # \r moves cursor to start; flush ensures it appears immediately
                sys.stdout.write(f"\r{line.strip()}")
                sys.stdout.flush()
            else:
                # Clear the existing progress line: \r + 80 spaces + \r
                sys.stdout.write("\r" + " " * 80 + "\r")
                # Ensure the line ends with a newline for standard output
                print(line, end="\n" if not str(line).endswith("\n") else "")
                
