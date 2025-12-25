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

        max_lines = self.console.height - 10

        self.layout.split_row(
            Layout(name="flask", ratio=1),
            Layout(name="main", ratio=2),
        )

        self._lock = Lock()
        self._max_lines = max_lines

        # ring buffers of lines
        self._flask_lines = deque(maxlen=max_lines)
        self._main_lines = deque(maxlen=max_lines)

        self._render()

    def _render(self):
        flask_text = Text("\n".join(self._flask_lines))
        main_text = Text("\n".join(self._main_lines))

        self.layout["flask"].update(Panel(flask_text, title="Flask"))
        self.layout["main"].update(Panel(main_text, title="Main Script"))

    def log_flask(self, msg: str):
        with self._lock:
            self._flask_lines.append(msg)
            self._render()

    def log_main(self, msg: str):
        with self._lock:
            self._main_lines.append(msg)
            self._render()
