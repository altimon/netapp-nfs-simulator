#!/usr/bin/env python3
"""
End-to-end test: for each scenario, inject it, verify broken state on simulator
and real NFS server, apply the canonical fix, verify both sides recover.

Run on Rocky Linux as root:
  sudo python3 /opt/netapp-sim/test_scenarios.py

Credentials and IPs are read from /opt/netapp-sim/config.env — never hardcoded here.
"""
import json, subprocess, sys, os
sys.path.insert(0, "/opt/netapp-sim")
os.environ["NETAPP_STATE"] = "/opt/netapp-sim/state.json"

from simulator import load_state, dispatch, save_state
from tasks import get_task, cleanup_all, _load_config

cfg = _load_config()
EXPORT_DIR   = cfg.get("NFS_EXPORT_DIR",   "/srv/netapp/interview_test")
CLIENT_IP    = cfg.get("NFS_CLIENT_IP",    "")
CLIENT_USER  = cfg.get("NFS_CLIENT_USER",  "user")
CLIENT_PASS  = cfg.get("NFS_CLIENT_PASS",  "")
SERVER_IP    = cfg.get("NFS_SERVER_IP",    "")
NFS_SUBNET   = cfg.get("NFS_SUBNET",       "192.168.1.0/24")

# ── helpers ───────────────────────────────────────────────────────────────────

def exports():
    return subprocess.run("cat /etc/exports", shell=True, capture_output=True, text=True).stdout.strip()

def nfs_active():
    return subprocess.run("systemctl is-active nfs-server", shell=True, capture_output=True, text=True).stdout.strip()

def fw_has_nfs():
    return "nfs" in subprocess.run("firewall-cmd --list-services", shell=True, capture_output=True, text=True).stdout

def dir_perms():
    return oct(os.stat(EXPORT_DIR).st_mode)[-3:]

def dir_owner():
    import pwd
    return pwd.getpwuid(os.stat(EXPORT_DIR).st_uid).pw_name

def rad_rule(state):
    return state["svms"][0]["export_policies"]["rad_nfs_policy"]["rules"][0]

def vol(state, name="interview_test"):
    return next(v for v in state["volumes"] if v["name"] == name)

def do_dispatch(cmd):
    state = load_state()
    dispatch(cmd, state)
    return load_state()

def reset():
    state = load_state()
    cleanup_all(state)

PASS = FAIL = 0

def check(label, actual, expected):
    global PASS, FAIL
    ok = actual == expected
    print(f"  [{'✓' if ok else '✗'}] {label}")
    if not ok:
        print(f"        expected: {expected!r}")
        print(f"        got:      {actual!r}")
        FAIL += 1
    else:
        PASS += 1

def check_in(label, actual, substring):
    check(label, substring in actual, True)

def check_not_in(label, actual, substring):
    check(label, substring not in actual, True)

# ── Scenario 1: bad clientmatch ───────────────────────────────────────────────
print("\n══ Scenario 1: NFS export missing client subnet ══")
state = load_state()
get_task(state, ["storage", "1"])
state = load_state()
print("  -- broken --")
check("sim clientmatch=10.99.0.0/24", rad_rule(state)["clientmatch"], "10.99.0.0/24")
check_in("exports has 10.99", exports(), "10.99.0.0/24")
print("  -- fix --")
state = do_dispatch(f"vserver export-policy rule modify -vserver vs_parn_interview -policyname rad_nfs_policy -ruleindex 1 -clientmatch {NFS_SUBNET}")
check("sim clientmatch restored", rad_rule(state)["clientmatch"], NFS_SUBNET)
check_in("exports restored", exports(), NFS_SUBNET)
check_in("exports rw", exports(), "(rw,")

# ── Scenario 2: read-only policy ──────────────────────────────────────────────
print("\n══ Scenario 2: Export policy is read-only ══")
reset(); state = load_state()
get_task(state, ["storage", "2"])
state = load_state()
print("  -- broken --")
check("sim rw_rule=never", rad_rule(state)["rw_rule"], "never")
check_in("exports has ro", exports(), "(ro,")
print("  -- fix --")
state = do_dispatch("vserver export-policy rule modify -vserver vs_parn_interview -policyname rad_nfs_policy -ruleindex 1 -rwrule sys")
check("sim rw_rule=sys", rad_rule(state)["rw_rule"], "sys")
check_in("exports has rw", exports(), "(rw,")

# ── Scenario 3: volume offline ────────────────────────────────────────────────
print("\n══ Scenario 3: Volume interview_test is offline ══")
reset(); state = load_state()
get_task(state, ["storage", "3"])
state = load_state()
print("  -- broken --")
check("sim volume=offline", vol(state)["state"], "offline")
check("exports cleared", exports(), "")
print("  -- fix --")
state = do_dispatch("volume online -vserver vs_parn_interview -volume interview_test")
check("sim volume=online", vol(state)["state"], "online")
check_in("exports restored", exports(), EXPORT_DIR)
check_in("exports rw", exports(), "(rw,")

# ── Scenario 4: wrong junction path ───────────────────────────────────────────
print("\n══ Scenario 4: Wrong junction path ══")
reset(); state = load_state()
get_task(state, ["storage", "4"])
state = load_state()
print("  -- broken --")
check("sim junction_path wrong", vol(state)["junction_path"], "/wrong_interview_path")
print("  -- fix --")
state = do_dispatch("volume mount -vserver vs_parn_interview -volume interview_test -junction-path /interview_test")
check("sim junction_path restored", vol(state)["junction_path"], "/interview_test")

# ── Scenario 5: firewall blocks NFS ───────────────────────────────────────────
print("\n══ Scenario 5: Linux firewall blocks NFS ══")
reset(); state = load_state()
get_task(state, ["storage", "5"])
print("  -- broken --")
check("firewall nfs blocked", fw_has_nfs(), False)
print("  -- fix (server-side, outside ONTAP CLI) --")
subprocess.run("firewall-cmd --add-service=nfs --add-service=mountd --add-service=rpc-bind 2>/dev/null", shell=True)
check("firewall nfs restored", fw_has_nfs(), True)

# ── Scenario 6: NFS service stopped ───────────────────────────────────────────
print("\n══ Scenario 6: NFS service stopped ══")
reset(); state = load_state()
get_task(state, ["storage", "6"])
print("  -- broken --")
check("nfs-server stopped", nfs_active(), "inactive")
print("  -- fix (server-side, outside ONTAP CLI) --")
subprocess.run("systemctl start nfs-server", shell=True)
check("nfs-server running", nfs_active(), "active")

# ── Scenario 7: client mounts wrong path ──────────────────────────────────────
print("\n══ Scenario 7: Client mounts wrong path ══")
reset(); state = load_state()
get_task(state, ["storage", "7"])
print("  -- broken (checking Ubuntu client) --")
if CLIENT_IP and CLIENT_PASS:
    r = subprocess.run(
        f"sshpass -p '{CLIENT_PASS}' ssh -o StrictHostKeyChecking=no {CLIENT_USER}@{CLIENT_IP} "
        f"'findmnt /mnt --output SOURCE --noheadings 2>/dev/null || echo none'",
        shell=True, capture_output=True, text=True
    )
    mnt_src = r.stdout.strip()
    correct = f"{SERVER_IP}:{EXPORT_DIR}"
    print(f"  [i] /mnt source on Ubuntu: {mnt_src!r}")
    check("client not on correct interview_test path", mnt_src != correct, True)
else:
    print("  [!] NFS_CLIENT_IP/NFS_CLIENT_PASS not set — skipping client check")

# ── Scenario 8: no superuser / permissions ────────────────────────────────────
print("\n══ Scenario 8: Client can mount but cannot write ══")
reset(); state = load_state()
get_task(state, ["storage", "8"])
state = load_state()
print("  -- broken --")
check("sim super_user=none", rad_rule(state)["super_user"], "none")
check_in("exports has root_squash", exports(), "root_squash")
check("export dir perms 755", dir_perms(), "755")
check("export dir owner root", dir_owner(), "root")
print("  -- fix --")
state = do_dispatch("vserver export-policy rule modify -vserver vs_parn_interview -policyname rad_nfs_policy -ruleindex 1 -superuser sys")
check("sim super_user=sys", rad_rule(state)["super_user"], "sys")
check_in("exports has no_root_squash", exports(), "no_root_squash")

# ── Scenario 9: vmware_nfs read-only ──────────────────────────────────────────
print("\n══ Scenario 9: vol_vmware_nfs exported read-only ══")
reset(); state = load_state()
get_task(state, ["storage", "9"])
state = load_state()
vmware_rule = state["svms"][0]["export_policies"]["vmware_nfs_policy"]["rules"][0]
print("  -- broken --")
check("sim vmware rw_rule=never", vmware_rule["rw_rule"], "never")
print("  -- fix --")
state = do_dispatch("vserver export-policy rule modify -vserver vs_parn_interview -policyname vmware_nfs_policy -ruleindex 1 -rwrule sys")
vmware_rule = state["svms"][0]["export_policies"]["vmware_nfs_policy"]["rules"][0]
check("sim vmware rw_rule=sys", vmware_rule["rw_rule"], "sys")

# ── Scenario 10: wrong LIF IP ─────────────────────────────────────────────────
print("\n══ Scenario 10: Data LIF on wrong subnet ══")
reset(); state = load_state()
get_task(state, ["storage", "10"])
state = load_state()
data_lif = next(l for l in state["lifs"] if "data_lif1" in l["name"])
print("  -- broken --")
check("sim data LIF IP=10.10.30.42", data_lif["address"], "10.10.30.42")
print("  -- fix --")
state = do_dispatch(f"network interface modify -vserver vs_parn_interview -lif vs_parn_interview_data_lif1 -address {SERVER_IP}")
data_lif = next(l for l in state["lifs"] if "data_lif1" in l["name"])
check("sim data LIF IP restored", data_lif["address"], SERVER_IP)

# ── final cleanup ─────────────────────────────────────────────────────────────
print("\n══ Final cleanup ══")
reset()
state = load_state()
check("clean clientmatch", rad_rule(state)["clientmatch"], NFS_SUBNET)
check("clean rw_rule", rad_rule(state)["rw_rule"], "sys")
check("clean volume online", vol(state)["state"], "online")
check_in("clean exports rw", exports(), "(rw,")
check_in("clean exports subnet", exports(), NFS_SUBNET)
check("clean nfs-server running", nfs_active(), "active")
check("clean firewall has nfs", fw_has_nfs(), True)

print(f"\n{'═'*55}")
print(f"  TOTAL: {PASS} passed, {FAIL} failed")
print(f"{'═'*55}\n")
sys.exit(0 if FAIL == 0 else 1)
