# test/test_release_pipeline.py
"""Properties of the release workflow itself.

Each of these silently stops being true if someone edits the YAML, and nothing
else in the suite would notice. This file is copied into every service that
uses the template, so a property broken here is broken everywhere.
"""
import os
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/release-pipeline.yml"
#: The packaging logic lives in a script so CI and the local runner execute the
#: same code; assertions about packaging belong against it, not the YAML.
PACKAGER = ROOT / "scripts/package.sh"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _jobs(workflow):
    return workflow["jobs"]


def _steps(job):
    return job["steps"]


def _all_steps(workflow):
    return [s for job in _jobs(workflow).values() for s in _steps(job)]


def _run(step) -> str:
    """A step's shell, with comment lines removed.

    Searching raw `run:` text finds matches inside comments. A step whose
    comment mentions `gh release create` is not a step that runs it, and an
    assertion fooled that way passes whatever the command actually says.
    """
    lines = str(step.get("run", "")).splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _step_running(workflow, fragment):
    found = [s for s in _all_steps(workflow) if fragment in _run(s)]
    assert len(found) == 1, f"expected exactly one step running {fragment!r}, found {len(found)}"
    return found[0]


def test_nothing_but_a_push_to_prod_triggers_a_release(workflow):
    """Three of the service repositories are PUBLIC. A `pull_request` trigger
    would let a fork's pull request reach a workflow that holds the shared
    SERVICE signing key -- and a key in one public repo is a key compromised
    for all eight. `push` requires write access, which a fork does not have.
    """
    # PyYAML parses a bare `on:` key as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert list(triggers) == ["push"]
    assert triggers["push"]["branches"] == ["prod"]


def test_only_one_job_can_see_the_signing_key(workflow):
    """The build matrix runs on more than one runner. The key is needed on
    exactly one of them, so every runner that does not need it must not receive
    it -- extra exposure buys nothing."""
    signing = [name for name, job in _jobs(workflow).items()
               if "SERVICE_SIGNING_KEY" in str(job)]
    assert signing == ["release"]
    steps = [s.get("name") for s in _all_steps(workflow)
             if "SERVICE_SIGNING_KEY" in str(s.get("env", {}))]
    assert len(steps) == 1, f"more than one step receives the key: {steps}"


def test_the_build_job_receives_no_secrets(workflow):
    assert "secrets." not in str(_jobs(workflow)["build"])


def test_the_signing_step_runs_the_repository_signer(workflow):
    """Not an inline `python -c`: the checks that make signing safe -- the key
    matching what this repository expects, the round-trip verify -- live in
    scripts/sign.py and are tested. A step that signed inline would have none
    of them and would look identical in the log."""
    step = _step_running(workflow, "scripts/sign.py")
    assert "SERVICE_SIGNING_KEY" in step.get("env", {})


def test_no_run_block_interpolates_into_a_shell(workflow):
    """A repository name or tag containing $(...) would otherwise execute on
    the runner. Checking for `${{` rather than a specific expression closes the
    class rather than one instance of it.

    The build job's steps are exempt: `matrix.platform` is a value this file
    defines itself, and it reaches paths that have no env-var equivalent.
    """
    for name, job in _jobs(workflow).items():
        if name == "build":
            continue
        for step in _steps(job):
            assert "${{" not in _run(step), \
                f"step {step.get('name')!r} interpolates into a shell"


def test_the_release_is_published_with_the_signatures(workflow):
    """A release carrying zips and no `.sig` files verifies for nobody, and
    every step before this one would still have passed."""
    step = _step_running(workflow, "gh release create")
    assert "upload/*.sig" in _run(step)
    assert "upload/*.zip" in _run(step)
    assert "SHA256SUMS" in _run(step)


def test_an_empty_download_is_not_published_as_a_release():
    """`gh release create` with no files succeeds and publishes an empty
    release. Without this guard a failed matrix leg produces a release a
    customer can download nothing from."""
    assert re.search(r'"\$found"\s+-eq\s+0', PACKAGER.read_text()), \
        "nothing checks that any platform was actually packaged"


def test_artefact_names_come_from_the_repository(workflow):
    """The two hand-copied pipelines in this org differ by exactly four lines,
    all of them the service name. Deriving it means a service author copies
    this file unchanged and that entire class of error disappears."""
    step = _step_running(workflow, "scripts/package.sh")
    assert step["env"]["SERVICE"] == "${{ github.event.repository.name }}"
    assert "${SERVICE}-${platform}.zip" in PACKAGER.read_text()


def test_the_workflow_packages_through_the_shared_script(workflow):
    """Not inline shell. What packaging repairs is damage done between the
    build job and this one, so it can only be tested by simulating that
    round-trip and running the real thing -- which means CI and
    docker-local/build-binary.sh have to execute the same file."""
    step = _step_running(workflow, "scripts/package.sh")
    assert PACKAGER.is_file(), "scripts/package.sh is missing"
    assert "zip" not in _run(step), "packaging logic has leaked back into the YAML"


def test_the_build_action_is_pinned_to_a_commit(workflow):
    """This action turns source into the binary a customer runs. Tracking a
    moving branch means the shipped artefact can change without any commit
    here, and without anyone reviewing the change."""
    builds = [s for s in _all_steps(workflow)
              if "python-binary-action" in str(s.get("uses", ""))]
    assert builds, "no build step found"
    for step in builds:
        ref = step["uses"].split("@", 1)[1]
        assert re.fullmatch(r"[0-9a-f]{40}", ref), \
            f"build action is pinned to {ref!r}, not a commit SHA"


def test_the_release_job_waits_for_every_platform(workflow):
    assert _jobs(workflow)["release"]["needs"] == "build"


def test_the_workflow_can_write_releases_and_no_more(workflow):
    assert workflow["permissions"] == {"contents": "write"}


def test_runtime_requirements_carry_no_test_tooling():
    """Everything in requirements.txt is installed into the release build and
    ends up inside the shipped binary. A test framework in there is shipped to
    every customer of every service that copies this template."""
    runtime = (WORKFLOW.parent.parent.parent / "requirements.txt").read_text()
    for package in ("pytest", "pluggy", "Pygments"):
        assert package.lower() not in runtime.lower(), \
            f"{package} is a runtime dependency and would be built into the binary"


def test_the_build_puts_app_on_the_import_path(workflow):
    """`app/main.py` does `from dependencies import ...`, which works when
    Python runs the script because a script's own directory goes on sys.path.
    PyInstaller is invoked from the repository root, so it analyses main.py
    without `app/` on the search path, fails to resolve `dependencies`, and
    still produces a binary -- one that dies on its first import.

    Nothing else catches this. The build succeeds, the artefact is the right
    size, and only running it reveals the problem.
    """
    builds = [s for s in _all_steps(workflow)
              if "python-binary-action" in str(s.get("uses", ""))]
    assert builds, "no build step found"
    for step in builds:
        assert "--paths app" in str(step["with"].get("additional-args", "")), \
            "PyInstaller is not given app/ as an import path"


def test_the_release_output_paths_are_gitignored():
    """The release workflow writes binaries to `dist/` and zips to `upload/`.
    In CI that is a throwaway runner, but anyone running the pipeline locally
    leaves both in their working tree -- where `git add -A` commits a 12MB
    binary, and nothing in the diff makes that obvious.

    Coupled to the workflow by nothing except this test: change an output path
    there and the ignore silently stops covering it.
    """
    ignored = (WORKFLOW.parent.parent.parent / ".gitignore").read_text().split()
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    for path in ("dist/", "upload/"):
        assert path in ignored, f"{path} is not gitignored"
        assert path.rstrip("/") in workflow_text, \
            f"{path} is ignored but the workflow no longer writes there"


def test_the_upload_keeps_hidden_files(workflow):
    """`logs/` is kept by a `.gitkeep`, and actions/upload-artifact@v4 excludes
    dotfiles unless told otherwise. Without this the file is dropped, the
    directory is then empty, and an empty directory is not stored either -- so
    `logs/` silently never reaches the customer, while every step still passes.

    That is not hypothetical: release prod-1 shipped without it."""
    uploads = [s for s in _all_steps(workflow)
               if "upload-artifact" in str(s.get("uses", ""))]
    assert uploads, "no upload step found"
    for step in uploads:
        assert step["with"].get("include-hidden-files") is True, \
            "the upload drops dotfiles, so logs/ will not reach the customer"


def test_the_binary_is_made_executable_before_zipping():
    """actions/upload-artifact cannot preserve the executable bit, so the binary
    arrives at packaging as 0644 and zips as 0644. The customer unzips something
    they cannot run, and no privilege fixes it -- Linux requires at least one x
    bit even for root.

    prod-1 shipped a `-rw-r--r--` binary. Every job was green."""
    source = PACKAGER.read_text()
    assert "chmod +x" in source, "nothing restores the executable bit"
    # Chained to the declared entry script, not a hardcoded name, so renaming
    # the script cannot leave this chmod-ing a file that no longer exists.
    assert 'basename "$SCRIPT_PATH"' in source


def test_packaging_ships_no_logs_directory():
    """Logging goes over MQTT: services print, the orchestrator tees to
    project/logging/<service>, and logging-service writes the files. A logs/
    folder here would ship empty to every customer, which prod-4 did."""
    assert 'mkdir -p "$dir/logs"' not in PACKAGER.read_text()


def test_the_entry_script_is_declared_once(workflow):
    """The build passes it to PyInstaller and packaging derives the binary name
    from it. Two copies would disagree silently the day a service renames it."""
    assert workflow["env"]["SCRIPT"] == "app/main.py"
    builds = [s for s in _all_steps(workflow)
              if "python-binary-action" in str(s.get("uses", ""))]
    for step in builds:
        assert step["with"]["scripts"] == "${{ env.SCRIPT }}"


def test_the_packaging_script_is_executable():
    """The workflow invokes it as `./scripts/package.sh`, so a copy committed
    without its executable bit fails the release with "Permission denied" --
    after the build has already run on three platforms. Git records the mode,
    and a file recreated by an editor or a careless `cp` loses it silently."""
    assert os.access(PACKAGER, os.X_OK), \
        "scripts/package.sh is not executable; the release step cannot run it"


def test_macos_is_not_built_without_a_signing_certificate(workflow):
    """No customer runs macOS, and an unsigned macOS binary is refused by
    Gatekeeper on first launch -- "cannot be opened because the developer
    cannot be verified" -- which only an Apple Developer certificate solves.

    Adding the leg back without that certificate ships three platforms and
    delivers two. If you are here because this test failed, the certificate is
    the prerequisite, not this assertion."""
    platforms = [leg["platform"]
                 for leg in workflow["jobs"]["build"]["strategy"]["matrix"]["include"]]
    assert not [p for p in platforms if p.startswith("macos")], \
        f"macOS is being built: {platforms}"
