"""Compose the legacy CLI with granular task command groups."""

from .cli import app
from .commands_tasks import pipeline_app, stage_app

app.add_typer(stage_app, name="stage")
app.add_typer(pipeline_app, name="pipeline")

__all__ = ["app"]
