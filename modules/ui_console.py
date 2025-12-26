from collections import deque
from threading import Lock
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text


class UISplit:
    def __init__(self):
        self.console = Console()
        self.layout = Layout()

        self.layout.split_row(
            Layout(name="flask", ratio=1),
            Layout(name="main", ratio=2),
        )

        self._lock = Lock()

        # Keep a lot of history; we’ll slice to what fits on-screen at render time.
        self._flask_lines = deque(maxlen=5000)
        self._main_lines = deque(maxlen=5000)

        self._render()

    def _visible_line_count(self) -> int:
        # Panel borders + title eat a couple lines. Give it a little padding.
        h = self.console.height
        return max(5, h - 4)

    def _render(self):
        max_lines = self._visible_line_count()

        flask_tail = list(self._flask_lines)[-max_lines:]
        main_tail = list(self._main_lines)[-max_lines:]

        flask_text = Text("\n".join(flask_tail))
        main_text = Text("\n".join(main_tail))

        self.layout["flask"].update(Panel(flask_text, title="Flask"))
        self.layout["main"].update(Panel(main_text, title="Main Script"))

    def _append_lines(self, buf: deque, msg: str):
        # Split multi-line messages so the deque is truly line-based
        lines = msg.splitlines() or [msg]
        for line in lines:
            buf.append(line)

    def log_flask(self, msg: str):
        with self._lock:
            self._append_lines(self._flask_lines, msg)
            self._render()

    def log_main(self, msg: str):
        with self._lock:
            self._append_lines(self._main_lines, msg)
            self._render()
