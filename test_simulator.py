#!/usr/bin/env python3
"""
Integration tests for simulator.py.
Run: python3 test_simulator.py
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("NETAPP_STATE",    "/tmp/test_netapp_state.json")
os.environ.setdefault("NETAPP_HOST_KEY", "/tmp/test_netapp_host_key")

import simulator

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

_results = []

def assert_ok(name, condition, detail=""):
    mark = PASS if condition else FAIL
    print(f"  [{mark}] {name}" + (f": {detail}" if detail and not condition else ""))
    _results.append(condition)

def fresh_state() -> dict:
    with (Path(__file__).parent / "state.json").open() as f:
        s = json.load(f)
    s["session"] = {"commands_run": []}
    return s

def run(state, cmd):
    return simulator.dispatch(cmd, state)

# ── helpers ───────────────────────────────────────────────────────────────────

def _vol(state, name):
    return next((v for v in state["volumes"] if v["name"] == name), None)

# ── test suites ───────────────────────────────────────────────────────────────

def test_volume_show_fields():
    print("\nvolume show -fields policy")
    state = fresh_state()

    out = run(state, "volume show -vserver vs_parn_interview -volume interview_test "
                     "-fields junction-path,policy,state")
    assert_ok("output contains 'Policy' header",    "Policy" in out)
    assert_ok("output contains export policy name", "rad_nfs_policy" in out)
    assert_ok("output contains junction path",      "/interview_test" in out)
    assert_ok("output contains state",              "online" in out)


def test_volume_modify_policy_persists():
    print("\nvolume modify -policy persists")
    state = fresh_state()

    # Detach policy first so we can re-attach it
    vol = _vol(state, "interview_test")
    vol["export_policy"] = ""

    out = run(state, "volume modify -vserver vs_parn_interview "
                     "-volume interview_test -policy rad_nfs_policy")
    assert_ok("command succeeds", "successful" in out.lower(), out.strip())

    vol = _vol(state, "interview_test")
    assert_ok("export_policy field updated in state",
              vol["export_policy"] == "rad_nfs_policy",
              repr(vol["export_policy"]))

    out2 = run(state, "volume show -vserver vs_parn_interview -volume interview_test "
                      "-fields junction-path,policy,state")
    assert_ok("volume show -fields policy shows rad_nfs_policy", "rad_nfs_policy" in out2, out2)


def test_volume_modify_invalid_policy():
    print("\nvolume modify -policy invalid name → error")
    state = fresh_state()

    out = run(state, "volume modify -vserver vs_parn_interview "
                     "-volume interview_test -policy nonexistent_policy")
    assert_ok("returns error on bad policy",  "Error" in out or "error" in out, out.strip())
    assert_ok("mentions policy name in error", "nonexistent_policy" in out)

    vol = _vol(state, "interview_test")
    assert_ok("export_policy unchanged after bad modify",
              vol["export_policy"] == "rad_nfs_policy")


def test_volume_modify_other_fields_unaffected():
    print("\nvolume modify -policy leaves other fields intact")
    state = fresh_state()
    vol_before = dict(_vol(state, "interview_test"))

    run(state, "volume modify -vserver vs_parn_interview "
               "-volume interview_test -policy rad_nfs_policy")

    vol_after = _vol(state, "interview_test")
    for field in ("name", "vserver", "aggregate", "size", "state", "type",
                  "security_style", "junction_path"):
        assert_ok(f"field '{field}' unchanged",
                  vol_after[field] == vol_before[field],
                  f"{vol_after[field]!r} != {vol_before[field]!r}")


def test_volume_show_fields_all():
    print("\nvolume show -fields (all supported fields)")
    state = fresh_state()
    out = run(state, "volume show -fields "
                     "vserver,volume,size,aggregate,state,type,security-style,junction-path,policy")
    for expected in ("Vserver", "Volume", "Size", "Aggregate", "State",
                     "Security", "Junction Path", "Policy"):
        assert_ok(f"header '{expected}' present", expected in out)


def test_volume_modify_junction_and_policy_together():
    print("\nvolume modify -junction-path and -policy together")
    state = fresh_state()
    vol = _vol(state, "interview_test")
    vol["junction_path"] = "/wrong_path"
    vol["export_policy"] = ""

    out = run(state, "volume modify -vserver vs_parn_interview -volume interview_test "
                     "-junction-path /interview_test -policy rad_nfs_policy")
    assert_ok("command succeeds", "successful" in out.lower())

    vol = _vol(state, "interview_test")
    assert_ok("junction_path updated", vol["junction_path"] == "/interview_test")
    assert_ok("export_policy updated", vol["export_policy"] == "rad_nfs_policy")

    out2 = run(state, "volume show -vserver vs_parn_interview -volume interview_test "
                      "-fields junction-path,policy,state")
    assert_ok("both fields visible in volume show", "rad_nfs_policy" in out2 and "/interview_test" in out2)


def test_show_commands_smoke():
    print("\nsmoke: all show commands return output")
    state = fresh_state()
    cases = [
        "version",
        "system node show",
        "aggr show",
        "storage aggregate show",
        "volume show",
        "network interface show",
        "vserver show",
        "vserver nfs show",
        "vserver export-policy show",
        "vserver export-policy rule show",
        "export-policy rule show",
        "qtree show",
        "df -h",
    ]
    for cmd in cases:
        out = run(state, cmd)
        assert_ok(cmd, out.strip() != "" and out != "__EXIT__")


# ── runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_volume_show_fields()
    test_volume_modify_policy_persists()
    test_volume_modify_invalid_policy()
    test_volume_modify_other_fields_unaffected()
    test_volume_show_fields_all()
    test_volume_modify_junction_and_policy_together()
    test_show_commands_smoke()

    total  = len(_results)
    passed = sum(_results)
    failed = total - passed
    print(f"\n{'='*40}")
    print(f"Results: {passed}/{total} passed", end="")
    if failed:
        print(f"  ({failed} FAILED)")
        sys.exit(1)
    else:
        print(" — all OK")
