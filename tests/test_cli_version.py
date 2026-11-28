import importlib.metadata

import pytest

from lights_out.cli import main


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert importlib.metadata.version("lights-out") in capsys.readouterr().out
