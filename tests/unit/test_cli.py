import pytest

from logdash import __version__
from logdash.cli import main


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_main_without_args_succeeds():
    assert main([]) == 0
