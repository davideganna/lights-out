from importlib.metadata import version

import app


def test_version_matches_installed_metadata():
    assert isinstance(app.__version__, str)
    assert app.__version__
    assert app.__version__ == version("app")
