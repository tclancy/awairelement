"""`restart.sh` must restart every unit this repo ships (#139).

`itguy deploy awairelement` is a git pull plus this script, so a unit the
script does not name keeps running the code it started with — indefinitely,
and with nothing to say so. The script used to carry a hand-written list of
three, and the mini split poller was the fourth.

Asserted as the *derivation* rather than as today's four names: a test that
listed the units would need the same edit the script did, and would go green
on exactly the change it exists to catch.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RESTART = REPO / "restart.sh"


def units_in_repo():
    return sorted(p.stem for p in (REPO / "systemd").glob("*.service"))


def test_the_repo_ships_units_for_this_test_to_be_about():
    """Reachability control: an empty `systemd/` would pass everything below."""
    assert len(units_in_repo()) >= 2


def test_the_restart_loop_names_no_unit_literally():
    """A literal unit name in the loop is the list this test exists to forbid."""
    script = RESTART.read_text()
    loop = re.search(r"^for unit in (.*); do$", script, re.MULTILINE)
    assert loop, "no `for unit in ...; do` loop in restart.sh"
    for unit in units_in_repo():
        assert unit not in loop.group(1), (
            f"restart.sh's loop names {unit} literally — derive the list from "
            "systemd/*.service so unit five cannot be forgotten"
        )


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs bash")
def test_the_loop_expands_to_every_unit_in_the_repo():
    """Run the loop's own expansion, so this is the script's answer, not ours."""
    script = RESTART.read_text()
    expansion = re.search(r"^for unit in (.*); do$", script, re.MULTILINE).group(1)
    out = subprocess.run(
        ["/bin/bash", "-c", f'for unit in {expansion}; do echo "$unit"; done'],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert sorted(out.stdout.split()) == units_in_repo()
