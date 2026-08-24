"""DevOpsPipeline — an extensible Python CI/CD platform.

Top-level package metadata. Subpackages (orchestrator, runners, stages,
plugins, security, artifacts, metrics, dashboard) are imported explicitly to
keep startup light.
"""

from __future__ import annotations

__version__ = "1.0.0"
__author__ = "DevOpsPipeline Maintainers"
__all__ = ["__version__", "__author__"]
