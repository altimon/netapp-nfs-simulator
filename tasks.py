#!/usr/bin/env python3
"""
Task engine for NetApp ONTAP simulator.
Injects broken scenarios and grades the session.
Handles real cleanup/setup on both Rocky (NFS server) and Ubuntu (NFS client).
"""

import asyncio
import concurrent.futures
import json
import os
import random
import subprocess
from pathlib import Path
from textwrap import dedent

# ── config loader ─────────────────────────────────────────────────────────────

def _load_config() -> dict:
    cfg_path = Path(os.environ.get("NETAPP_STATE", "/opt/netapp-sim/state.json")).parent / "config.env"
    cfg = {}
    if cfg_path.exists():
        for line in cfg_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    return cfg

# ── remote execution helpers ──────────────────────────────────────────────────

def _run_local(cmds: list) -> list:
    """Run shell commands locally (on Rocky as root). Returns list of (cmd, rc, out, err)."""
    results = []
    for cmd in (cmds if isinstance(cmds, list) else [cmds]):
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        results.append((cmd, r.returncode, r.stdout.strip(), r.stderr.strip()))
    return results


async def _ssh_run_async(host, user, password, cmds: list):
    try:
        import asyncssh
    except ImportError:
        return []
    results = []
    async with asyncssh.connect(
        host, username=user, password=password,
        known_hosts=None, connect_timeout=8
    ) as conn:
        for cmd in cmds:
            r = await conn.run(cmd, check=False)
            results.append((cmd, r.exit_status, (r.stdout or "").strip(), (r.stderr or "").strip()))
    return results


def _run_remote(host, user, password, cmds: list) -> list:
    """Run commands on a remote host via SSH. Best-effort — errors are swallowed.

    Uses a thread so asyncio.run() gets a fresh event loop even when called
    from inside asyncssh's already-running loop.
    """
    if not host or not password:
        return []
    def _thread():
        return asyncio.run(_ssh_run_async(host, user, password, cmds))
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_thread).result(timeout=30)
    except Exception as e:
        return [("ssh", -1, "", str(e))]

# ── server-side cleanup (Rocky — runs locally as root) ───────────────────────

def _cleanup_server(cfg: dict):
    export_dir = cfg.get("NFS_EXPORT_DIR", "/srv/netapp/interview_test")
    nfs_subnet = cfg.get("NFS_SUBNET", "192.168.1.0/24")

    _run_local([
        # Restore NFS service
        "systemctl start nfs-server",
        # Restore /etc/exports
        f"printf '%s  %s(rw,sync,no_subtree_check,no_root_squash)\\n' "
        f"'{export_dir}' '{nfs_subnet}' > /etc/exports",
        "exportfs -ra",
        # Restore firewall NFS rules (runtime only — permanent rules already set)
        "firewall-cmd --add-service=nfs   2>/dev/null || true",
        "firewall-cmd --add-service=mountd 2>/dev/null || true",
        "firewall-cmd --add-service=rpc-bind 2>/dev/null || true",
        # Restore export directory permissions and wipe leftover files
        f"mkdir -p {export_dir}",
        f"chown nobody:nobody {export_dir}",
        f"chmod 0777 {export_dir}",
        f"rm -f {export_dir}/*",
    ])

# ── client-side cleanup (Ubuntu — runs via SSH) ───────────────────────────────

def _cleanup_client(cfg: dict):
    host  = cfg.get("NFS_CLIENT_IP")
    user  = cfg.get("NFS_CLIENT_USER", "user")
    passwd = cfg.get("NFS_CLIENT_PASS")
    mount = cfg.get("NFS_CLIENT_MOUNT", "/mnt")

    if not host or not passwd:
        return

    sudo = f"echo '{passwd}' | sudo -S"
    _run_remote(host, user, passwd, [
        # Force-unmount /mnt if mounted (ignore errors)
        f"mountpoint -q {mount} && {sudo} umount -f -l {mount} 2>/dev/null || true",
        # Remove any leftover test files
        f"rm -f {mount}/hello_world 2>/dev/null || true",
    ])

# ── NFS sync: push simulator state → real /etc/exports ───────────────────────

def sync_nfs_from_state(state: dict):
    """After any ONTAP config fix, reflect it in the real /etc/exports on Rocky."""
    cfg = _load_config()
    export_dir = cfg.get("NFS_EXPORT_DIR", "/srv/netapp/interview_test")

    vol = next((v for v in state.get("volumes", []) if v["name"] == "interview_test"), None)
    if not vol:
        return

    # Volume offline or unmounted → remove export
    if vol.get("state") == "offline" or not vol.get("junction_path"):
        _run_local(["truncate -s 0 /etc/exports", "exportfs -ra"])
        return

    svm = next((s for s in state.get("svms", []) if s["name"] == vol.get("vserver")), None)
    if not svm:
        return

    policy_name = vol.get("export_policy", "rad_nfs_policy")
    policy = svm.get("export_policies", {}).get(policy_name)

    if not policy or not policy.get("rules"):
        _run_local(["truncate -s 0 /etc/exports", "exportfs -ra"])
        return

    rule = policy["rules"][0]
    clientmatch = rule.get("clientmatch", "0.0.0.0/0")
    rw_opt     = "rw" if rule.get("rw_rule") not in ("never", None) else "ro"
    squash_opt = "no_root_squash" if rule.get("super_user") not in ("none", None) else "root_squash"

    cmds = [
        f"printf '%s  %s({rw_opt},sync,no_subtree_check,{squash_opt})\\n' "
        f"'{export_dir}' '{clientmatch}' > /etc/exports",
        "exportfs -ra",
    ]

    # Restore dir ownership/perms when write access is allowed.
    # Scenario 8 sets chown root:root + chmod 0755 — undo that here.
    if rw_opt == "rw" and squash_opt == "no_root_squash":
        cmds += [
            f"chown nobody:nobody {export_dir}",
            f"chmod 0777 {export_dir}",
        ]

    _run_local(cmds)

# ── full cleanup (state + server + client) ────────────────────────────────────

def cleanup_all(state: dict) -> str:
    cfg = _load_config()
    msgs = ["", "  Cleaning up previous session..."]

    _cleanup_server(cfg)
    msgs.append("  [✓] NFS server reset (exports, service, firewall, permissions)")

    client_ip = cfg.get("NFS_CLIENT_IP")
    if client_ip:
        _cleanup_client(cfg)
        msgs.append(f"  [✓] NFS client reset ({client_ip} /mnt unmounted)")
    else:
        msgs.append("  [!] NFS_CLIENT_IP not set in config.env — client cleanup skipped")

    # Reset simulator state to defaults
    from simulator import save_state, DEFAULT_STATE_PATH
    if DEFAULT_STATE_PATH.exists():
        with DEFAULT_STATE_PATH.open() as f:
            defaults = json.load(f)
        # Restore install-time values from config.env (ground truth, not current state)
        data_lif_ip  = cfg.get("DATA_LIF_IP")
        admin_lif_ip = cfg.get("ADMIN_LIF_IP")
        for lif in defaults.get("lifs", []):
            if data_lif_ip and "data" in lif["name"]:
                lif["address"] = data_lif_ip
            elif admin_lif_ip and "admin" in lif["name"]:
                lif["address"] = admin_lif_ip
        nfs_subnet = cfg.get("NFS_SUBNET")
        if nfs_subnet:
            for svm in defaults.get("svms", []):
                pol = svm.get("export_policies", {}).get("rad_nfs_policy")
                if pol:
                    for rule in pol["rules"]:
                        rule["clientmatch"] = nfs_subnet
        defaults["session"] = {"commands_run": [], "client_steps_reported": []}
        state.clear()
        state.update(defaults)
        save_state(state)
        msgs.append("  [✓] Simulator state reset to defaults")
    else:
        msgs.append("  [!] state.default.json not found — state not reset")

    msgs.append("")
    return "\n".join(msgs)

# ── scenario definitions ─────────────────────────────────────────────────────

SCENARIOS = [
    {
        "id": 1,
        "title": "NFS export missing client subnet",
        "description": dedent("""\
            Users on the lab subnet are unable to mount the NFS export
            for interview_test. The storage admin reports that 'showmount -e'
            returns no matching exports from the Ubuntu client.
        """),
        "hint": "Check the export policy rules. Something about the clientmatch may be wrong.",
        "inject_sim":    lambda state: _inject_bad_clientmatch(state),
        "inject_server": lambda cfg: _inject_bad_clientmatch_server(cfg),
        "inject_client": None,
    },
    {
        "id": 2,
        "title": "Export policy is read-only",
        "description": dedent("""\
            The NFS share mounts successfully from the client, but users cannot
            create files. They receive 'Permission denied' on write operations.
        """),
        "hint": "Verify the export policy rule's rw_rule setting.",
        "inject_sim":    lambda state: _inject_readonly_policy(state),
        "inject_server": lambda cfg: _inject_readonly_server(cfg),
        "inject_client": None,
    },
    {
        "id": 3,
        "title": "Volume interview_test is offline",
        "description": dedent("""\
            The interview_test volume is not accessible. Clients cannot mount
            the NFS export and report a 'No such file or directory' error.
        """),
        "hint": "Check the volume state. An offline volume will not serve data.",
        "inject_sim":    lambda state: _inject_volume_offline(state),
        "inject_server": lambda cfg: _inject_volume_offline_server(cfg),
        "inject_client": None,
    },
    {
        "id": 4,
        "title": "Wrong junction path",
        "description": dedent("""\
            The NFS client can connect but mounts the wrong directory. Files
            written by one team are not visible to another team mounting the
            expected path /interview_test.
        """),
        "hint": "Verify the junction-path for the interview_test volume.",
        "inject_sim":    lambda state: _inject_wrong_junction(state),
        "inject_server": None,
        "inject_client": None,
    },
    {
        "id": 5,
        "title": "Linux firewall blocks NFS",
        "description": dedent("""\
            The NFS server is running and the export is configured correctly,
            but clients cannot connect. The mount command hangs and times out.
            On the Ubuntu client, 'showmount -e' fails with:
              clnt_create: RPC: Unable to receive
            This means RPC ports (111, mountd) are unreachable — not an ONTAP issue.
        """),
        "hint": "This is a network-layer issue on the NFS server. Check firewall rules on Rocky.",
        "inject_sim":    None,
        "inject_server": lambda cfg: _inject_firewall_block(cfg),
        "inject_client": None,
    },
    {
        "id": 6,
        "title": "NFS service stopped on server",
        "description": dedent("""\
            The Rocky Linux server is reachable but the NFS mount fails.
            The export directory exists and /etc/exports is correctly configured.
        """),
        "hint": "The NFS daemon itself may not be running.",
        "inject_sim":    None,
        "inject_server": lambda cfg: _inject_nfs_stopped(cfg),
        "inject_client": None,
    },
    {
        "id": 7,
        "title": "Client mounts wrong path",
        "description": dedent("""\
            The NFS client has /mnt mounted but it points to the wrong export path.
            Files written are not visible to other users expecting /interview_test.
        """),
        "hint": "On the client, check what is actually mounted. Use findmnt or mount.",
        "inject_sim":    None,
        "inject_server": None,
        "inject_client": lambda cfg: _inject_wrong_client_mount(cfg),
    },
    {
        "id": 8,
        "title": "Client can mount but cannot write (permissions)",
        "description": dedent("""\
            The NFS share mounts fine and the export policy allows rw, but
            the client user gets 'Permission denied' when writing. The policy
            shows superuser=none.
        """),
        "hint": "Look at the export rule's superuser setting and the filesystem permissions.",
        "inject_sim":    lambda state: _inject_no_superuser(state),
        "inject_server": lambda cfg: _inject_permissions_server(cfg),
        "inject_client": None,
    },
    {
        "id": 9,
        "title": "vol_vmware_nfs exported read-only by mistake",
        "description": dedent("""\
            The VMware team reports they can mount vol_vmware_nfs but all
            write operations fail with 'Permission denied'. The NFS export
            was recently modified.

            Note: interview_test and rad_nfs_policy are unaffected —
            the problem is on a different volume and policy.
            This scenario is ONTAP CLI only; verify the fix by inspecting
            the policy, not by mounting.
        """),
        "hint": dedent("""\
            1. Find which export policy vol_vmware_nfs uses:
                 volume show -vserver vs_parn_interview -volume vol_vmware_nfs -fields policy
            2. Inspect that policy's rules:
                 vserver export-policy rule show -vserver vs_parn_interview -policyname vmware_nfs_policy -fields clientmatch,rorule,rwrule,superuser
            3. Fix the rw_rule:
                 vserver export-policy rule modify -vserver vs_parn_interview \\
                   -policyname vmware_nfs_policy -ruleindex 1 -rwrule sys
        """),
        "inject_sim":    lambda state: _inject_vmware_readonly(state),
        "inject_server": None,
        "inject_client": None,
    },
    {
        "id": 10,
        "title": "Data LIF shown on wrong subnet",
        "description": dedent("""\
            The data LIF for vs_parn_interview appears to be on the
            10.10.30.0/24 subnet instead of the lab subnet.
            NFS clients on the lab subnet cannot reach the storage.
        """),
        "hint": "Check the LIF IP address with 'network interface show'.",
        "inject_sim":    lambda state: _inject_wrong_lif_ip(state),
        "inject_server": None,
        "inject_client": None,
    },
]

# ── simulator-side injectors ─────────────────────────────────────────────────

def _inject_bad_clientmatch(state):
    svm = state["svms"][0]
    svm["export_policies"]["rad_nfs_policy"]["rules"][0]["clientmatch"] = "10.99.0.0/24"

def _inject_readonly_policy(state):
    svm = state["svms"][0]
    svm["export_policies"]["rad_nfs_policy"]["rules"][0]["rw_rule"] = "never"

def _inject_volume_offline(state):
    for v in state["volumes"]:
        if v["name"] == "interview_test":
            v["state"] = "offline"
            break

def _inject_wrong_junction(state):
    for v in state["volumes"]:
        if v["name"] == "interview_test":
            v["junction_path"] = "/wrong_interview_path"
            break

def _inject_no_superuser(state):
    svm = state["svms"][0]
    rule = svm["export_policies"]["rad_nfs_policy"]["rules"][0]
    rule["super_user"] = "none"
    rule["rw_rule"] = "sys"

def _inject_vmware_readonly(state):
    svm = state["svms"][0]
    svm["export_policies"]["vmware_nfs_policy"]["rules"][0]["rw_rule"] = "never"

def _inject_wrong_lif_ip(state):
    for lif in state["lifs"]:
        if lif["name"] == "vs_parn_interview_data_lif1":
            lif["address"] = "10.10.30.42"
            break

# ── server-side injectors (Rocky — local subprocess) ─────────────────────────

def _inject_bad_clientmatch_server(cfg):
    export_dir = cfg.get("NFS_EXPORT_DIR", "/srv/netapp/interview_test")
    _run_local([
        f"printf '%s  10.99.0.0/24(rw,sync,no_subtree_check,no_root_squash)\\n' '{export_dir}' > /etc/exports",
        "exportfs -ra",
    ])

def _inject_readonly_server(cfg):
    export_dir = cfg.get("NFS_EXPORT_DIR", "/srv/netapp/interview_test")
    nfs_subnet = cfg.get("NFS_SUBNET", "192.168.1.0/24")
    _run_local([
        f"printf '%s  %s(ro,sync,no_subtree_check,no_root_squash)\\n' '{export_dir}' '{nfs_subnet}' > /etc/exports",
        "exportfs -ra",
    ])

def _inject_volume_offline_server(cfg):
    _run_local([
        "truncate -s 0 /etc/exports",
        "exportfs -ra",
    ])

def _inject_permissions_server(cfg):
    export_dir = cfg.get("NFS_EXPORT_DIR", "/srv/netapp/interview_test")
    nfs_subnet = cfg.get("NFS_SUBNET", "192.168.1.0/24")
    _run_local([
        f"printf '%s  %s(rw,sync,no_subtree_check,root_squash)\\n' '{export_dir}' '{nfs_subnet}' > /etc/exports",
        "exportfs -ra",
        f"chown root:root {export_dir}",
        f"chmod 0755 {export_dir}",
    ])

def _inject_firewall_block(cfg):
    _run_local([
        "firewall-cmd --remove-service=nfs    2>/dev/null || true",
        "firewall-cmd --remove-service=mountd  2>/dev/null || true",
        "firewall-cmd --remove-service=rpc-bind 2>/dev/null || true",
    ])

def _inject_nfs_stopped(cfg):
    _run_local(["systemctl stop nfs-server"])

# ── client-side injectors (Ubuntu — SSH) ─────────────────────────────────────

def _inject_wrong_client_mount(cfg):
    host   = cfg.get("NFS_CLIENT_IP")
    user   = cfg.get("NFS_CLIENT_USER", "user")
    passwd = cfg.get("NFS_CLIENT_PASS")
    server = cfg.get("NFS_SERVER_IP", "192.168.1.10")
    mount  = cfg.get("NFS_CLIENT_MOUNT", "/mnt")
    export_dir = cfg.get("NFS_EXPORT_DIR", "/srv/netapp/interview_test")

    if not host or not passwd:
        return

    sudo = f"echo '{passwd}' | sudo -S"
    # Mount the NFS root instead of the specific export path
    _run_remote(host, user, passwd, [
        f"mountpoint -q {mount} && {sudo} umount -f -l {mount} 2>/dev/null || true",
        f"{sudo} mount -t nfs {server}:{export_dir}/.. {mount} 2>/dev/null || "
        f"{sudo} mount -t nfs {server}:/ {mount} 2>/dev/null || true",
    ])

# ── public API ────────────────────────────────────────────────────────────────

CLIENT_STEPS = {
    "showmount": "Ran showmount -e on Linux client",
    "mount":     "Mounted NFS on /mnt",
    "findmnt":   "Verified with findmnt or nfsstat",
    "touch":     "Created hello_world file",
    "verify":    "Verified read/write",
}


def _list_tasks() -> str:
    lines = [
        "",
        "  Available scenarios:",
        "  ────────────────────────────────────────────────────",
    ]
    for s in SCENARIOS:
        lines.append(f"  {s['id']:>2}.  {s['title']}")
    lines += [
        "  ────────────────────────────────────────────────────",
        "  Usage:",
        "    task storage        - load a random scenario",
        "    task storage <1-10> - load a specific scenario",
        "    task list           - show this list",
        "",
    ]
    return "\n".join(lines)


def get_task(state: dict, args: list) -> str:
    if args and args[0] == "list":
        return _list_tasks()

    scenario_id = None
    for token in args:
        if token.isdigit():
            scenario_id = int(token)
            break

    if scenario_id is not None:
        scenario = next((s for s in SCENARIOS if s["id"] == scenario_id), None)
        if scenario is None:
            valid = ", ".join(str(s["id"]) for s in SCENARIOS)
            return f"\nError: scenario {scenario_id} not found. Valid IDs: {valid}\n"
    else:
        scenario = random.choice(SCENARIOS)

    # Full cleanup before injecting the new scenario
    cleanup_msg = cleanup_all(state)

    cfg = _load_config()

    # Apply injections
    if scenario.get("inject_sim"):
        scenario["inject_sim"](state)
        from simulator import save_state
        save_state(state)

    if scenario.get("inject_server"):
        scenario["inject_server"](cfg)

    if scenario.get("inject_client"):
        scenario["inject_client"](cfg)

    state["session"]["active_scenario"] = scenario["id"]
    state["session"]["commands_run"] = []
    from simulator import save_state
    save_state(state)

    output = [
        cleanup_msg.rstrip(),
        "",
        f"  ══════════════════════════════════════════════════════",
        f"  TASK #{scenario['id']}: {scenario['title']}",
        f"  ══════════════════════════════════════════════════════",
        "",
        "  DESCRIPTION:",
    ]
    for line in scenario["description"].strip().splitlines():
        output.append(f"    {line}")
    output.append("")
    output.append("  Investigate and resolve the issue.")
    output.append("  Type 'grade' when finished.\n")
    return "\n".join(output)


def report_step(state: dict, args: list) -> str:
    if not args or args[0] == "list":
        reported = set(state["session"].get("client_steps_reported", []))
        lines = [
            "",
            "  Client-side steps (self-reported):",
            "  ─────────────────────────────────────────────",
        ]
        for key, label in CLIENT_STEPS.items():
            mark = "✓" if key in reported else "✗"
            lines.append(f"  [{mark}] {key:<12}  {label}")
        lines += [
            "  ─────────────────────────────────────────────",
            "  Usage: report <step>  |  report all",
            "  Steps: " + "  ".join(CLIENT_STEPS.keys()),
            "",
        ]
        return "\n".join(lines)

    key = args[0].lower()

    if key == "all":
        reported = state["session"].setdefault("client_steps_reported", [])
        added = [k for k in CLIENT_STEPS if k not in reported]
        reported.extend(added)
        from simulator import save_state
        save_state(state)
        lines = ["", "  [✓] All client steps recorded:"]
        for k, label in CLIENT_STEPS.items():
            lines.append(f"      • {label}")
        lines.append("")
        return "\n".join(lines)

    if key not in CLIENT_STEPS:
        valid = ", ".join(CLIENT_STEPS.keys()) + ", all"
        return f"\nUnknown step '{key}'. Valid steps: {valid}\n"

    reported = state["session"].setdefault("client_steps_reported", [])
    if key not in reported:
        reported.append(key)
        from simulator import save_state
        save_state(state)
    return f"\n  [✓] Recorded: {CLIENT_STEPS[key]}\n"


def _live_fw_has_nfs():
    r = subprocess.run("firewall-cmd --list-services", shell=True, capture_output=True, text=True)
    return "nfs" in r.stdout

def _live_nfs_running():
    r = subprocess.run("systemctl is-active nfs-server", shell=True, capture_output=True, text=True)
    return r.stdout.strip() == "active"

def _live_client_mount_correct():
    cfg = _load_config()
    host   = cfg.get("NFS_CLIENT_IP")
    user   = cfg.get("NFS_CLIENT_USER", "user")
    passwd = cfg.get("NFS_CLIENT_PASS")
    server = cfg.get("NFS_SERVER_IP", "192.168.1.10")
    export = cfg.get("NFS_EXPORT_DIR", "/srv/netapp/interview_test")
    mount  = cfg.get("NFS_CLIENT_MOUNT", "/mnt")
    if not host or not passwd:
        return False
    results = _run_remote(host, user, passwd, [
        f"findmnt {mount} --output SOURCE --noheadings 2>/dev/null || echo none"
    ])
    if not results:
        return False
    src = results[0][2].strip()
    return src == f"{server}:{export}"


SCENARIO_EXTRA_CHECKS = {
    9: [
        (
            "Identified vol_vmware_nfs volume and its policy",
            ("vol_vmware_nfs",),
            ["volume show -vserver vs_parn_interview -volume vol_vmware_nfs -fields policy"],
        ),
        (
            "Inspected vmware_nfs_policy rules",
            ("vmware_nfs_policy",),
            ["vserver export-policy rule show -vserver vs_parn_interview -policyname vmware_nfs_policy"],
        ),
        (
            "Fixed vmware_nfs_policy rw_rule (export-policy rule modify)",
            ("vmware_nfs_policy",),
            ["vserver export-policy rule modify -vserver vs_parn_interview "
             "-policyname vmware_nfs_policy -ruleindex 1 -rwrule sys"],
        ),
    ],
    4: [
        (
            "Unmounted the volume (volume unmount)",
            ("volume unmount",),
            ["volume unmount -vserver vs_parn_interview -volume interview_test"],
        ),
        (
            "Remounted with correct junction path (volume mount)",
            ("volume mount",),
            ["volume mount -vserver vs_parn_interview -volume interview_test -junction-path /interview_test"],
        ),
    ],
    5: [
        (
            "Firewall NFS rules restored on Rocky (nfs/mountd/rpc-bind)",
            _live_fw_has_nfs,
            ["[Rocky] sudo firewall-cmd --add-service=nfs --add-service=mountd --add-service=rpc-bind"],
        ),
        (
            "NFS client can now mount (verified from Ubuntu)",
            _live_client_mount_correct,
            ["[Ubuntu] sudo mount -t nfs <rocky-ip>:/srv/netapp/interview_test /mnt",
             "Then: report mount"],
        ),
    ],
    6: [
        (
            "NFS service restarted on Rocky",
            _live_nfs_running,
            ["[Rocky] sudo systemctl start nfs-server"],
        ),
        (
            "NFS client can now mount (verified from Ubuntu)",
            _live_client_mount_correct,
            ["[Ubuntu] sudo mount -t nfs <rocky-ip>:/srv/netapp/interview_test /mnt",
             "Then: report mount"],
        ),
    ],
    7: [
        (
            "Client /mnt remounted to correct path",
            _live_client_mount_correct,
            ["[Ubuntu] sudo umount /mnt",
             "[Ubuntu] sudo mount -t nfs <rocky-ip>:/srv/netapp/interview_test /mnt"],
        ),
    ],
}


def grade_session(state: dict) -> str:
    cmds = [c.lower() for c in state["session"].get("commands_run", [])]
    reported = set(state["session"].get("client_steps_reported", []))

    def ran(*patterns):
        return any(any(p in c for p in patterns) for c in cmds)

    checks = [
        (
            "Inspected the volume (volume show)",
            ran("volume show"),
            ["volume show -vserver vs_parn_interview -volume interview_test"],
        ),
        (
            "Found volume size",
            ran("volume show -fields", "df"),
            ["volume show -fields vserver,volume,size,aggregate,state,junction-path,policy"],
        ),
        (
            "Found aggregate name",
            ran("volume show", "aggr show", "storage aggregate"),
            ["aggr show", "volume show -fields vserver,volume,aggregate"],
        ),
        (
            "Checked SVM LIF/IP (network interface show)",
            ran("network interface show"),
            ["network interface show -vserver vs_parn_interview"],
        ),
        (
            "Checked export policy (vserver export-policy rule show)",
            ran("export-policy rule show", "export-policy show"),
            ["vserver export-policy rule show -vserver vs_parn_interview"],
        ),
        (
            "Verified clientmatch/subnet",
            ran("export-policy rule show"),
            ["vserver export-policy rule show -vserver vs_parn_interview -policyname rad_nfs_policy"],
        ),
        (
            "Ran showmount -e on Linux client",
            "showmount" in reported,
            ["[Ubuntu] showmount -e <rocky-ip>", "Then: report showmount"],
        ),
        (
            "Mounted NFS on /mnt",
            "mount" in reported,
            ["[Ubuntu] sudo mount -t nfs <rocky-ip>:/srv/netapp/interview_test /mnt",
             "Then: report mount"],
        ),
        (
            "Verified with findmnt or nfsstat",
            "findmnt" in reported,
            ["[Ubuntu] findmnt /mnt   OR   nfsstat -m", "Then: report findmnt"],
        ),
        (
            "Created hello_world file",
            "touch" in reported,
            ["[Ubuntu] touch /mnt/hello_world", "Then: report touch"],
        ),
        (
            "Verified read/write",
            "verify" in reported,
            ["[Ubuntu] ls -l /mnt/hello_world", "Then: report verify"],
        ),
    ]

    # Append scenario-specific checks
    # check_spec is either a tuple of ran() patterns or a callable for a live check
    active = state["session"].get("active_scenario")
    if active in SCENARIO_EXTRA_CHECKS:
        for label, check_spec, hints in SCENARIO_EXTRA_CHECKS[active]:
            result = check_spec() if callable(check_spec) else ran(*check_spec)
            checks.append((label, result, hints))

    lines = [
        "",
        "  ══════════════════════════════════════════════════════",
        "  SESSION GRADE REPORT",
        "  ══════════════════════════════════════════════════════",
        "",
    ]

    passed = 0
    for label, result, hints in checks:
        mark = "✓" if result else "✗"
        lines.append(f"  [{mark}] {label}")
        if result:
            passed += 1
        else:
            for hint in hints:
                lines.append(f"        → {hint}")

    pct = int(passed / len(checks) * 100)
    lines += [
        "",
        f"  Score: {passed}/{len(checks)} ({pct}%)",
        "",
        "  Tip: use 'report <step>' to record completed client steps.",
        "       Run 'report list' to see all reportable steps.",
        "",
    ]
    return "\n".join(lines)
