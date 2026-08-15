"""T-11: a built wheel must be self-sufficient (no reliance on a repo checkout)."""
import email
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


def test_wheel_contains_the_pi_guard_extension_assets(built_wheel):
    """T-59 (PI-7): the pi guard extension + its checker + the destructive-
    command predicate it reuses (a symlink into `.claude/hooks/`, dereferenced
    into real file content by hatchling) must all ship as package data --
    `maestro.pi_guard.install` resolves them via `importlib.resources` against
    the INSTALLED package, so a real `pip install` (no repo checkout) must
    carry real files here, not a dangling symlink target."""
    z = zipfile.ZipFile(built_wheel)
    names = z.namelist()
    assert "maestro/_assets/pi/pi_guard_extension.ts" in names
    assert "maestro/_assets/pi/pi_guard_check.py" in names
    assert "maestro/_assets/pi/destructive_command_guard.py" in names
    # Real, non-empty predicate content -- not an unresolved symlink entry.
    content = z.read("maestro/_assets/pi/destructive_command_guard.py")
    assert b"PROTECTED_RELATIVE" in content


def test_wheel_declares_no_unconditional_runtime_dependencies(built_wheel):
    """RB-10: Hypothesis (and pytest, textual) must land in the built wheel's metadata ONLY as
    extra-gated `Requires-Dist` lines -- never as a bare, unconditional one -- so a fresh `pip
    install .` (no extras) pulls nothing new. Inspecting the actual built metadata, not just
    `pyproject.toml`'s source text, is what `hatchling` really emits into the wheel a user
    installs."""
    names = zipfile.ZipFile(built_wheel).namelist()
    info_name = next(n for n in names if n.endswith(".dist-info/METADATA"))
    metadata = email.message_from_bytes(zipfile.ZipFile(built_wheel).read(info_name))
    requires = metadata.get_all("Requires-Dist") or []
    unconditional = [r for r in requires if "extra ==" not in r]
    assert unconditional == []  # the core wheel's dependency list is empty
    assert any(r.startswith("hypothesis") and "extra == 'dev'" in r for r in requires)


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
