"""Simulation-specific exceptions."""


class SimulationError(Exception):
    """Base simulation error."""


class SimulationConfigurationError(SimulationError):
    """Invalid simulation configuration."""


class SimulationExecutionError(SimulationError):
    """Backend process execution failed."""


class SimulationResultParseError(SimulationError):
    """Backend output could not be parsed."""


class SimulationCancelledError(SimulationError):
    """Simulation execution was cancelled."""
