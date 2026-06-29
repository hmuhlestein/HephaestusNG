"""Main dashboard screen with tabs."""

from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane


class SystemStatus(Static):
    """Widget showing system status."""

    CSS = """
    SystemStatus {
        border: solid green;
        padding: 1;
        margin: 1;
        width: 40;
        height: 8;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.backend_status = False
        self.monitor_status = False
        self.qdrant_status = False
        self.database_status = False

    def render(self):
        """Render system status."""
        backend = "● Running" if self.backend_status else "○ Stopped"
        monitor = "● Running" if self.monitor_status else "○ Stopped"
        qdrant = "● Healthy" if self.qdrant_status else "○ Down"
        database = "● Ready" if self.database_status else "○ Error"

        return f"""[bold]System Status[/bold]

Backend     {backend}
Monitor     {monitor}
Qdrant      {qdrant}
Database    {database}"""

    def update_status(self, health: dict):
        """Update status from health check."""
        self.backend_status = health.get("backend_process", False)
        self.monitor_status = health.get("monitor_process", False)
        self.qdrant_status = health.get("qdrant", False)
        self.database_status = health.get("backend_api", False)
        self.refresh()


class QuickActions(Static):
    """Widget showing quick action hints."""

    CSS = """
    QuickActions {
        border: solid cyan;
        padding: 1;
        margin: 1;
        width: 40;
        height: 8;
    }
    """

    def render(self):
        return """[bold]Quick Actions[/bold]

[C] Create Task
[L] View Backend Logs
[O] View Monitor Logs
[R] Restart Services
[Q] Quit"""


class ActivityFeed(Static):
    """Widget showing recent activity."""

    CSS = """
    ActivityFeed {
        border: solid yellow;
        padding: 1;
        margin: 1;
        height: 12;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.activities = []

    def render(self):
        """Render recent activity."""
        content = "[bold]Recent Activity[/bold]\n\n"

        if not self.activities:
            content += "[dim]No recent activity[/dim]"
        else:
            for activity in self.activities[-5:]:  # Last 5 activities
                content += f"{activity}\n"

        return content

    def add_activity(self, activity: str):
        """Add an activity to the feed."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.activities.append(f"[dim]{timestamp}[/dim] {activity}")
        self.refresh()


class DashboardTab(Container):
    """Dashboard overview tab."""

    def compose(self) -> ComposeResult:
        """Compose the dashboard tab."""
        with Horizontal():
            with Vertical():
                yield SystemStatus(id="system-status")
                yield QuickActions(id="quick-actions")
            yield ActivityFeed(id="activity-feed")


class TasksTab(Container):
    """Tasks management tab."""

    def compose(self) -> ComposeResult:
        """Compose the tasks tab."""
        table = DataTable(id="tasks-table")
        table.add_columns("ID", "Description", "Status", "Agent", "Phase")
        yield Static(
            "[bold]Tasks[/bold]\n\nFilters: [All] Pending  In Progress  Done  Failed\n"
        )
        yield table


class AgentsTab(Container):
    """Agents monitoring tab."""

    def compose(self) -> ComposeResult:
        """Compose the agents tab."""
        table = DataTable(id="agents-table")
        table.add_columns("ID", "Task", "Status", "Created", "Last Active")
        yield Static("[bold]Active Agents[/bold]\n")
        yield table


class MemoryTab(Container):
    """Memory/RAG browser tab."""

    def compose(self) -> ComposeResult:
        """Compose the memory tab."""
        yield Static(
            "[bold]Memory Store[/bold]\n\n[dim]Search and browse stored memories[/dim]"
        )


class MetricsTab(Container):
    """Metrics and statistics tab."""

    def compose(self) -> ComposeResult:
        """Compose the metrics tab."""
        yield Static(
            "[bold]System Metrics[/bold]\n\n[dim]Performance metrics and statistics[/dim]"
        )


class DashboardScreen(Screen):
    """Main dashboard with tabbed interface."""

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("escape", "show_forge", "Back to Forge"),
        ("c", "create_task", "Create Task"),
        ("l", "view_backend_logs", "Backend Logs"),
        ("o", "view_monitor_logs", "Monitor Logs"),
    ]

    CSS = """
    DashboardScreen {
        background: $surface;
    }

    Header {
        background: $accent;
    }

    TabbedContent {
        height: 100%;
    }

    TabPane {
        padding: 1;
    }
    """

    def compose(self) -> ComposeResult:
        """Compose the dashboard."""
        yield Header(show_clock=True)

        with TabbedContent(initial="dashboard"):
            with TabPane("🔥 Dashboard", id="dashboard"):
                yield DashboardTab()

            with TabPane("📋 Tasks", id="tasks"):
                yield TasksTab()

            with TabPane("🤖 Agents", id="agents"):
                yield AgentsTab()

            with TabPane("💾 Memory", id="memory"):
                yield MemoryTab()

            with TabPane("📊 Metrics", id="metrics"):
                yield MetricsTab()

        yield Footer()

    def action_show_forge(self):
        """Return to forge screen."""
        from src.sdk.tui.screens.splash import SplashScreen

        self.app.push_screen(SplashScreen())

    def action_create_task(self):
        """Show create task dialog."""
        self.app.bell()  # TODO: Implement create task popup

    def action_view_backend_logs(self):
        """Show backend logs."""
        self.app.bell()  # TODO: Implement log viewer

    def action_view_monitor_logs(self):
        """Show monitor logs."""
        self.app.bell()  # TODO: Implement log viewer

    def action_quit(self):
        """Quit the application."""
        self.app.exit()
