"""RoCE GID-index selection (``scripts/_gid_index.sh``) — issue #327.

Without ``show_gids`` the old fallback took the first RoCE v2 sysfs entry, which
on ConnectX is index 1: the link-local ``fe80::`` GID.  NCCL bootstrapped and
then failed QP setup against peers addressed by IPv4-mapped GIDs.  When no
IPv4 GID could be found at all it pinned a hardcoded 3.

The helper is run for real under ``bash`` against a fixture sysfs tree with a
``PATH`` holding only the tools it needs (and, per test, a stub ``show_gids``),
since the defect lived entirely in shell fallback semantics.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from sparkrun.core.hardware_probe import generate_combined_probe_script
from sparkrun.orchestration.infiniband import generate_ib_detect_script
from sparkrun.scripts import read_script

_HCA = "rocep1s0f1"
_LINK_LOCAL = "fe80:0000:0000:0000:4ebb:47ff:fe1e:0a8b"
_IPV4_MAPPED = "0000:0000:0000:0000:0000:ffff:c0a8:640b"

# A ConnectX port with an IPv4 address: v1/v2 link-local, then v1/v2 IPv4.
_SPARK_PORT = {
    0: ("IB/RoCE v1", _LINK_LOCAL),
    1: ("RoCE v2", _LINK_LOCAL),
    2: ("IB/RoCE v1", _IPV4_MAPPED),
    3: ("RoCE v2", _IPV4_MAPPED),
}


def _make_port(root, entries, *, hca=_HCA, port_num="1"):
    port = root / hca / "ports" / str(port_num)
    (port / "gid_attrs" / "types").mkdir(parents=True)
    (port / "gids").mkdir(parents=True)
    for idx, (gid_type, gid) in entries.items():
        if gid_type is not None:
            (port / "gid_attrs" / "types" / str(idx)).write_text(gid_type + "\n")
        (port / "gids" / str(idx)).write_text(gid + "\n")
    return root / hca


@pytest.fixture
def bin_dir(tmp_path):
    """A PATH directory with only the tools the helper uses.

    Built rather than inherited so a host that really has ``show_gids``
    (any DGX Spark running the suite) cannot change the outcome.
    """
    d = tmp_path / "bin"
    d.mkdir()
    for tool in ("ls", "sort", "cat"):
        source = shutil.which(tool)
        if os.name == "nt":
            os.link(source, d / (tool + ".exe"))
        else:
            os.symlink(source, d / tool)
    # Shadow any host-installed show_gids, while retaining the original bin
    # path for DLL dependencies of the hard-linked Windows executables.
    stub = d / "show_gids"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    return d


def _stub_show_gids(bin_dir, output):
    stub = bin_dir / "show_gids"
    stub.write_text("#!/bin/sh\ncat <<'EOF'\n" + output + "EOF\n")
    stub.chmod(0o755)


def _run(bin_dir, hca_dir, hca=_HCA, port_num="1"):
    script = read_script("_gid_index.sh") + '\nsparkrun_rocev2_gid_index "$1" "$2" "$3"\n'
    tool_path = os.pathsep.join((str(bin_dir), os.path.dirname(shutil.which("bash"))))
    result = subprocess.run(
        [shutil.which("bash"), "-s", "--", hca, str(hca_dir), str(port_num)],
        input=script,
        capture_output=True,
        text=True,
        env={"PATH": tool_path},
        check=True,
    )
    return result.stdout.strip(), result.stderr


def test_sysfs_prefers_ipv4_gid_over_link_local(tmp_path, bin_dir):
    """The #327 regression: without show_gids, index 1 (fe80::) was chosen."""
    hca_dir = _make_port(tmp_path / "ib", _SPARK_PORT)
    idx, err = _run(bin_dir, hca_dir)
    assert idx == "3"
    assert "via sysfs" in err


def test_sysfs_selects_ipv4_gid_from_requested_port(tmp_path, bin_dir):
    hca_dir = _make_port(tmp_path / "ib", _SPARK_PORT, port_num="2")

    idx, err = _run(bin_dir, hca_dir, port_num="2")

    assert idx == "3"
    assert "via sysfs" in err


def test_ib_detect_reports_port2_when_port1_is_down(tmp_path, bin_dir):
    hca_dir = tmp_path / "sys" / "class" / "infiniband" / _HCA
    port1 = hca_dir / "ports" / "1"
    port1.mkdir(parents=True)
    (port1 / "state").write_text("4: DOWN\n")
    port2 = _make_port(tmp_path / "sys" / "class" / "infiniband", _SPARK_PORT, port_num="2") / "ports" / "2"
    (port2 / "state").write_text("4: ACTIVE\n")
    netdev = hca_dir / "device" / "net" / "cx7_1"
    netdev.mkdir(parents=True)
    net_sysfs = tmp_path / "net"
    net_sysfs.mkdir()

    script = generate_ib_detect_script().replace("/sys/class/infiniband", hca_dir.parent.as_posix())
    result = subprocess.run(
        [shutil.which("bash"), "-s"],
        input=script,
        capture_output=True,
        text=True,
        env={
            "PATH": os.pathsep.join((str(bin_dir), os.path.dirname(shutil.which("bash")))),
            "SPARKRUN_NET_SYSFS": net_sysfs.as_posix(),
        },
        check=True,
    )

    assert "IB_DETECTED=1" in result.stdout
    assert f"DETECTED_HCA_LIST={_HCA}:2" in result.stdout
    assert "DETECTED_UCX_LIST=" + _HCA + ":2" in result.stdout


def test_no_ipv4_gid_defers_to_nccl(tmp_path, bin_dir):
    """Only link-local GIDs present: hand selection to NCCL, never guess 3."""
    hca_dir = _make_port(tmp_path / "ib", {k: v for k, v in _SPARK_PORT.items() if k < 2})
    idx, err = _run(bin_dir, hca_dir)
    assert idx == "-1"
    assert "NCCL" in err


def test_missing_gid_tables_defers_to_nccl(tmp_path, bin_dir):
    hca_dir = tmp_path / "ib" / _HCA
    hca_dir.mkdir(parents=True)
    assert _run(bin_dir, hca_dir)[0] == "-1"


def test_ipv4_roce_v1_entry_is_not_accepted(tmp_path, bin_dir):
    hca_dir = _make_port(tmp_path / "ib", {0: ("IB/RoCE v1", _IPV4_MAPPED), 1: ("RoCE v2", _LINK_LOCAL)})
    assert _run(bin_dir, hca_dir)[0] == "-1"


def test_sysfs_scan_is_numeric_and_skips_unreadable_entries(tmp_path, bin_dir):
    """Index 3 wins over 10 (a glob would visit 10 first); typeless entries are skipped."""
    entries = {i: (None, "0000:0000:0000:0000:0000:0000:0000:0000") for i in range(12)}
    entries[3] = ("RoCE v2", _IPV4_MAPPED)
    entries[10] = ("RoCE v2", "0000:0000:0000:0000:0000:ffff:0a00:0001")
    hca_dir = _make_port(tmp_path / "ib", entries)
    assert _run(bin_dir, hca_dir)[0] == "3"


_SHOW_GIDS = f"""\
DEV\tPORT\tINDEX\tGID\t\t\t\t\tIPv4  \t\tVER\tDEV
---\t----\t-----\t---\t\t\t\t\t------------  \t---\t---
{_HCA}\t1\t0\t{_LINK_LOCAL}\t\t\tv1\tenp1s0f1np1
{_HCA}\t1\t1\t{_LINK_LOCAL}\t\t\tv2\tenp1s0f1np1
{_HCA}\t1\t2\t{_IPV4_MAPPED}\t192.168.100.11  \tv1\tenp1s0f1np1
{_HCA}\t1\t5\t{_IPV4_MAPPED}\t192.168.100.11  \tv2\tenp1s0f1np1
otherhca\t1\t4\t{_IPV4_MAPPED}\t192.168.101.11  \tv2\tenP2p1s0f1np1
n_gids_found=5
"""


def test_show_gids_answer_wins_over_sysfs(tmp_path, bin_dir):
    """show_gids stays first: the path every stock DGX OS host takes is unchanged."""
    _stub_show_gids(bin_dir, _SHOW_GIDS)
    hca_dir = _make_port(tmp_path / "ib", _SPARK_PORT)
    idx, err = _run(bin_dir, hca_dir)
    assert idx == "5"
    assert "via sysfs" not in err


def test_show_gids_without_ipv4_row_falls_through_to_sysfs(tmp_path, bin_dir):
    """A v2 row with an empty IPv4 column must not match (later columns shift left)."""
    rows = "\n".join(line for line in _SHOW_GIDS.splitlines() if "192.168" not in line) + "\n"
    _stub_show_gids(bin_dir, rows)
    hca_dir = _make_port(tmp_path / "ib", _SPARK_PORT)
    idx, err = _run(bin_dir, hca_dir)
    assert idx == "3"
    assert "via sysfs" in err


def test_helper_is_brace_free():
    """The combined probe passes the helper through str.format()."""
    helper = read_script("_gid_index.sh")
    assert "{" not in helper and "}" not in helper


@pytest.mark.parametrize("script", [generate_ib_detect_script(), generate_combined_probe_script()], ids=["ib_detect", "probe"])
def test_both_detectors_use_the_shared_helper(script):
    assert 'for port_path in "$ib_path"/ports/*' in script
    assert 'GID_INDEX=$(sparkrun_rocev2_gid_index "$hca_name" "$ib_path" "$port_num")' in script
    assert "DETECTED_HCA_LIST_EXACT=1" in script
    assert "defaulting to 3" not in script
    assert subprocess.run([shutil.which("bash"), "-n"], input=script, text=True).returncode == 0
