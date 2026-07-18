"""T-11: a built wheel must be self-sufficient (no reliance on a repo checkout)."""
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    """Build the wheel once and share it across this module's tests."""
    outdir = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(REPO_ROOT), "-w", str(outdir),
         "--no-deps", "-q"],
        check=True,
    )
    whl = next(outdir.glob("*.whl"))
    return whl


def test_wheel_contains_packaged_assets(built_wheel):
    """The daemon installer + zsh completion must ship as package data (T-11)."""
    names = zipfile.ZipFile(built_wheel).namelist()
    assert "maestro/_assets/daemon/install.sh" in names
    assert "maestro/_assets/completions/_maestro" in names


def test_wheel_install_smoke(built_wheel, tmp_path):
    """Installing the built wheel into a fresh venv gives a working `maestro env`."""
    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    venv_python = venv_dir / "bin" / "python"

    install = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "-q", str(built_wheel)],
        capture_output=True, text=True,
    )
    assert install.returncode == 0, install.stderr

    venv_maestro = venv_dir / "bin" / "maestro"
    result = subprocess.run(
        [str(venv_maestro), "env"], capture_output=True, text=True,
        env={"HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
