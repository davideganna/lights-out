"""lights-out: an autonomous software engineer driven by a Trello board."""

import importlib
import importlib.abc
import importlib.util
import sys

__version__ = "0.1.0"

# Modules that used to live at the top level of the package, mapped to their new location.
# `lights_out.agents` is intentionally absent: it is now a subpackage that re-exports the old names.
_MOVED = {
    "config": "lights_out.core.config",
    "models": "lights_out.core.models",
    "routing": "lights_out.core.routing",
    "trello": "lights_out.integrations.trello",
    "github": "lights_out.integrations.github",
    "prompts": "lights_out.agents.prompts",
    "pipeline": "lights_out.orchestration.pipeline",
    "scheduler": "lights_out.orchestration.scheduler",
    "workspace": "lights_out.orchestration.workspace",
    "store": "lights_out.reporting.store",
    "report": "lights_out.reporting.report",
}


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target: str) -> None:
        self._target = target

    def create_module(self, spec):
        return importlib.import_module(self._target)

    def exec_module(self, module) -> None:
        pass


class _MovedModuleFinder(importlib.abc.MetaPathFinder):
    """Make old import paths such as `lights_out.pipeline` resolve to the same module object as the new ones."""

    def find_spec(self, fullname, path=None, target=None):
        prefix, _, name = fullname.rpartition(".")
        if prefix != __name__ or name not in _MOVED:
            return None
        return importlib.util.spec_from_loader(fullname, _AliasLoader(_MOVED[name]))


if not any(isinstance(f, _MovedModuleFinder) for f in sys.meta_path):
    sys.meta_path.append(_MovedModuleFinder())
