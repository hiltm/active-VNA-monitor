# core/ui_helpers.py
import sys
import time
import threading

def wait_for_user_with_flash(message: str):
    """Flashes exclamation marks and banner until the user hits Enter."""
    stop_event = threading.Event()

    def _flash_worker():
        # ANSI Escape Codes: Red Bold (\033[91;1m), Yellow Bold (\033[93;1m), Reset (\033[0m)
        is_visible = True
        while not stop_event.is_set():
            bangs = "!!!" if is_visible else "   "
            sys.stdout.write(
                f"\r\033[K\033[91;1m{bangs}\033[0m "
                f"\033[93;1m{message}\033[0m "
                f"\033[91;1m{bangs}\033[0m  \033[90m[Press ENTER to proceed]\033[0m"
            )
            sys.stdout.flush()
            is_visible = not is_visible
            time.sleep(0.35)
        # Clear line on exit
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    flash_thread = threading.Thread(target=_flash_worker, daemon=True)
    flash_thread.start()
    
    # Wait for user keypress
    input()
    stop_event.set()
    flash_thread.join()