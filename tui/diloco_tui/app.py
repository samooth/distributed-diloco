"""DiLoCo TUI Dashboard."""
import asyncio
import httpx
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import DataTable, Static, Header, Footer, RichLog
from textual.binding import Binding

COORDINATOR_URL = "http://localhost:8095"


class DiLoCoDashboard(App):
    """Dashboard for DiLoCo distributed training."""

    CSS = """
    Screen { background: #0a0f1a; }
    Header { background: #1e293b; color: #e2e8f0; }
    Footer { background: #1e293b; color: #94a3b8; }
    DataTable { height: 10; }
    .metrics { padding: 1; background: #1e293b; color: #e2e8f0; }
    .logs { height: 8; border: solid #334155; }
    """

    BINDINGS = [
        Binding("s", "start", "Start"),
        Binding("p", "pause", "Pause"),
        Binding("r", "refresh", "Refresh"),
        Binding("l", "toggle_logs", "Logs"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, coordinator_url: str = COORDINATOR_URL):
        super().__init__()
        self.coordinator_url = coordinator_url
        self.client = httpx.AsyncClient(timeout=5.0)
        self.logs_visible = True

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container():
            with Horizontal():
                yield DataTable(id="nodes", zebra_stripes=True)
                yield DataTable(id="rounds", zebra_stripes=True)
            yield Static("Métricas: cargando...", id="metrics", classes="metrics")
            yield RichLog(id="logs", classes="logs", highlight=True)
        yield Footer()

    async def on_mount(self):
        self.set_interval(2.0, self.refresh_data)
        await self.refresh_data()

    async def refresh_data(self):
        try:
            status = await self.client.get(f"{self.coordinator_url}/status")
            status_data = status.json()
            # Update nodes table
            nodes_table = self.query_one("#nodes", DataTable)
            nodes_table.clear()
            nodes_table.add_columns("Node", "Estado", "Ronda", "CU")
            # Placeholder: would fetch from /nodes endpoint
            nodes_table.add_row("node-0", "active", "5", "87%")
            nodes_table.add_row("node-1", "active", "5", "92%")
            # Update rounds table
            rounds_table = self.query_one("#rounds", DataTable)
            rounds_table.clear()
            rounds_table.add_columns("Ronda", "Estado", "Deltas", "Deadline")
            rounds_table.add_row("5", "collecting", "3/4", "12:45")
            rounds_table.add_row("4", "completed", "4/4", "12:30")
            # Update metrics
            metrics = self.query_one("#metrics", Static)
            metrics.update(f"Loss gap: +5.26%  |  CU: 87%  |  Goodput: 12.3 tok/s  |  Bytes/token: 0.54 MB  |  Nodos: {status_data.get('nodes', 0)}")
            # Log
            log = self.query_one("#logs", RichLog)
            log.write(f"[{status_data.get('nodes', 0)} nodos, {status_data.get('rounds', 0)} rondas]")
        except Exception as e:
            log = self.query_one("#logs", RichLog)
            log.write(f"Error: {e}")

    async def action_start(self):
        try:
            await self.client.post(f"{self.coordinator_url}/start")
            self.query_one("#logs", RichLog).write("Entrenamiento iniciado")
        except Exception as e:
            self.query_one("#logs", RichLog).write(f"Error start: {e}")

    async def action_pause(self):
        try:
            await self.client.post(f"{self.coordinator_url}/pause")
            self.query_one("#logs", RichLog).write("Entrenamiento pausado")
        except Exception as e:
            self.query_one("#logs", RichLog).write(f"Error pause: {e}")

    async def action_refresh(self):
        await self.refresh_data()

    def action_toggle_logs(self):
        self.logs_visible = not self.logs_visible
        self.query_one("#logs", RichLog).display = self.logs_visible

    def action_quit(self):
        self.exit()

def main():
    import argparse
    parser = argparse.ArgumentParser(description="DiLoCo TUI Dashboard")
    parser.add_argument("--coordinator", default="http://localhost:8095", help="Coordinator URL")
    args = parser.parse_args()
    app = DiLoCoDashboard(coordinator_url=args.coordinator)
    app.run()


if __name__ == "__main__":
    main()
