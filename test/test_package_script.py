# test/test_package_script.py
"""scripts/package.sh, exercised the way the release workflow calls it.

This runs in CI on every pull request, which the docker-local tooling does not.
Both defects this file guards reached a published release: prod-1 shipped a
binary the customer could not execute and no logs/ directory, and the first
attempt at fixing that failed in CI on a relative out-dir the local test had
been passing absolutely.
"""
import os
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGER = ROOT / "scripts/package.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("zip") is None or shutil.which("bash") is None,
    reason="needs bash and zip; the release job runs on ubuntu")


@pytest.fixture
def built(tmp_path):
    """A build directory shaped like one an artifact download leaves behind.

    The binary is 0644 on purpose: actions/upload-artifact cannot preserve the
    executable bit, so this is exactly what packaging is handed.
    """
    d = tmp_path / "build" / "build-linux-amd64"
    d.mkdir(parents=True)
    binary = d / "main"
    binary.write_text("#!/bin/sh\necho hello\n")
    binary.chmod(0o644)
    (d / "config.yaml").write_text("broker_details: {}\n")
    return tmp_path


def _package(cwd, out="upload"):
    """Invoke the packager as the workflow does: relative paths, from the
    directory holding `build/`."""
    return subprocess.run(
        [str(PACKAGER), "build", out, "a-service", "app/main.py"],
        cwd=cwd, capture_output=True, text=True)


def test_a_relative_out_dir_works(built):
    """The workflow passes `upload`, not an absolute path. Resolving it after
    changing into the build directory yields nothing, and zip then tries to
    write to the filesystem root -- which is how the first fix for this file
    failed in CI while passing locally, where the test used absolute paths."""
    done = _package(built)
    assert done.returncode == 0, done.stderr
    assert (built / "upload" / "a-service-linux-amd64.zip").is_file()


def test_the_binary_is_executable_in_the_zip(built):
    """prod-1 shipped 0644. No privilege recovers it: Linux requires at least
    one x bit even for root."""
    assert _package(built).returncode == 0
    with zipfile.ZipFile(built / "upload" / "a-service-linux-amd64.zip") as z:
        mode = {i.filename: i.external_attr >> 16 for i in z.infolist()}
    assert mode["main"] & stat.S_IXUSR, "the binary is not executable in the zip"


def test_the_config_is_not_made_executable(built):
    """chmod +x is aimed at the binary by name, not sprayed over the folder."""
    assert _package(built).returncode == 0
    with zipfile.ZipFile(built / "upload" / "a-service-linux-amd64.zip") as z:
        mode = {i.filename: i.external_attr >> 16 for i in z.infolist()}
    assert not mode["config.yaml"] & stat.S_IXUSR


def test_no_logs_directory_is_shipped(built):
    """Services do not write log files. They print; the orchestrator tees every
    child's output to project/logging/<service>; logging-service is the only
    thing that writes to disk, under its own configured directory.

    A logs/ folder in every service package would be an empty directory nothing
    ever opens -- and prod-4 shipped exactly that, on every platform."""
    assert _package(built).returncode == 0
    with zipfile.ZipFile(built / "upload" / "a-service-linux-amd64.zip") as z:
        assert not [n for n in z.namelist() if n.startswith("logs")], z.namelist()


def test_a_windows_binary_is_found_by_its_exe_name(built):
    """The windows leg produces main.exe. Chmod-ing only `main` would leave the
    windows zip unchanged and nothing would say so."""
    win = built / "build" / "build-windows-amd64"
    win.mkdir(parents=True)
    (win / "main.exe").write_text("MZ")
    (win / "main.exe").chmod(0o644)
    (win / "config.yaml").write_text("{}\n")
    assert _package(built).returncode == 0
    with zipfile.ZipFile(built / "upload" / "a-service-windows-amd64.zip") as z:
        mode = {i.filename: i.external_attr >> 16 for i in z.infolist()}
    assert mode["main.exe"] & stat.S_IXUSR


def test_no_build_directories_is_an_error(tmp_path):
    """`gh release create` succeeds with no files, so a silent zero here
    publishes a release a customer can download nothing from."""
    (tmp_path / "build").mkdir()
    done = _package(tmp_path)
    assert done.returncode != 0
    assert "nothing to release" in (done.stdout + done.stderr)
