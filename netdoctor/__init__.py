"""NetDoctor network quality diagnostics."""

from .analysis import analyze
from .config import AppConfig, TargetConfig
from .measurement import NetworkMeasurer, run_diagnostics
from .models import DiagnosticAnalysis, MetricResult, RunResult, StatisticalSummary

__all__ = [
    "AppConfig",
    "DiagnosticAnalysis",
    "MetricResult",
    "NetworkMeasurer",
    "RunResult",
    "StatisticalSummary",
    "TargetConfig",
    "analyze",
    "run_diagnostics",
]

__version__ = "0.3.0"
