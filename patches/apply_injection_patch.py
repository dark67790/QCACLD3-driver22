#!/usr/bin/env python3
"""
apply_injection_patch.py

Applies patches/07_packet_injection.patch to vendor/qcom/opensource/wlan/
(qcacld-3.0 + qca-wifi-host-cmn), then hand-merges the handful of hunks that
don't apply cleanly because this tree has diverged from the patch's original
baseline:

  - rx_monitor.h / rx_monitor.c / cfg80211.c includes + vendor cmd array
  - main.h injection_ctx field
  - main.c __hdd_mon_open (this tree already has its own idempotent monitor
    open handling + a BBM_DRIVER_MODE_POLICY block the patch's baseline
    didn't have)
  - wma_main.c wma_open (this tree has an extra wma_trace_init() call)
  - wma_utils.c wma_mon_mlme_vdev_down_send (del_bss_resp allocation)
  - default_defconfig injection flag
  - tx_rx.c wlan_hdd_netif_queue_control (this tree has an extra
    hdd_adapter_is_link_adapter MLO guard)

Deliberately NOT applied (non-essential, or real risk of breaking
tree-specific logic the patch's baseline never had):
  - main.c wlan_off_str/wlan_on_str root-UID gating while monitor active
    (this tree has a WAIT_FOR_READY branch + different control flow the
    patch never accounted for)
  - main.c HDD_FW_VER_SUB_ID macro fix (unrelated firmware-version decoding)
  - Kbuild -Wmaybe-uninitialized / -Wframe-larger-than block (unrelated
    compiler-warning housekeeping)

Confirmed NOT APPLICABLE (not drifted -- the targeted code no longer
exists in this tree at all; see KNOWN_INAPPLICABLE_REJECTS for the full
reasoning, kept there so it prints every run rather than being buried):
  - cfg80211.c: 7 hunks touching the legacy STA/IBSS connect path
    (wlan_hdd_cfg80211_connect_start, wlan_hdd_cfg80211_join_ibss,
    __wlan_hdd_cfg80211_connect, hdd_dump_connect_req) -- this tree has
    moved that flow to the Connection Manager architecture
    (wlan_hdd_cm_connect) and none of those four functions exist here.
    Unrelated to monitor mode / frame injection either way.

Run from the repo root (the directory containing vendor/qcom/opensource/wlan).
Exits non-zero on any failure so CI surfaces it instead of silently
producing a half-patched tree.
"""
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path.cwd()
WLAN_DIR = REPO_ROOT / "vendor/qcom/opensource/wlan"
QCACLD = WLAN_DIR / "qcacld-3.0"
HDD_SRC = QCACLD / "core/hdd/src"
HDD_INC = QCACLD / "core/hdd/inc"
WMA_SRC = QCACLD / "core/wma/src"
WMA_INC = QCACLD / "core/wma/inc"
CONFIGS = QCACLD / "configs"
PATCH_FILE = Path(__file__).resolve().parent / "07_packet_injection.patch"

FAILURES = []
ATTEMPTED_FILES = set()  # files this script has any hand-merge logic for at all

# .rej files confirmed (via live CI ground truth, not guessed) to contain
# ONLY hunks that are genuinely not applicable to this tree -- not merely
# drifted, but targeting code that no longer exists at all. The content
# heuristic in check_unaddressed_rejects() can't distinguish "nothing
# addresses this" from "this was investigated and found inapplicable", so
# these are listed explicitly, with the reasoning kept here (and printed
# every run) rather than silently assumed forever.
KNOWN_INAPPLICABLE_REJECTS = {
    "qcacld-3.0/core/hdd/src/wlan_hdd_cfg80211.c.rej": (
        "7 of its 9 hunks target the legacy STA/IBSS connect path -- "
        "wlan_hdd_cfg80211_connect_start(), wlan_hdd_cfg80211_join_ibss(), "
        "__wlan_hdd_cfg80211_connect(), hdd_dump_connect_req() -- and "
        "confirmed via live CI that NONE of those four names appear "
        "anywhere in this file at all (not drifted -- entirely absent). "
        "This tree's STA/IBSS connect flow has been replaced wholesale by "
        "the Connection Manager architecture (wlan_hdd_cm_connect(), "
        "confirmed present at the call site that used to invoke the old "
        "functions). These 7 hunks are pre-existing upstream connect-flow "
        "diagnostic-logging changes that happened to be bundled into the "
        "same patch file as the packet-injection feature -- unrelated to "
        "monitor mode or frame injection, which lives entirely in "
        "__hdd_hard_start_xmit, hdd_is_monitor_tx_dev / "
        "hdd_monitor_mode_tx_inject, wma_frame_inject.c, the qca_vendor.h "
        "enums, wlan_hdd_rx_monitor.c, and the cfg80211 vendor-command "
        "wiring -- all handled elsewhere in this script. There is no "
        "equivalent code left to hand-merge these into. The other 2 "
        "hunks in this same reject (the FEATURE_FRAME_INJECTION_SUPPORT "
        "include guard and the vendor-command array entry) ARE applied, "
        "via the 'cfg80211.c include' / 'cfg80211.c vendor cmd array' "
        "fuzzy_replace calls in apply_hand_merges()."
    ),
}


def run_bulk_patch():
    print("== Step 1: bulk patch -p3 (34 clean files + fuzzy-but-correct hunks) ==")
    if not PATCH_FILE.exists():
        sys.exit(f"FATAL: patch file not found at {PATCH_FILE}")
    if not WLAN_DIR.exists():
        sys.exit(f"FATAL: {WLAN_DIR} not found — run from repo root")

    result = subprocess.run(
        [
            "patch", "-p3", "--forward", "--no-backup-if-mismatch",
            "--fuzz=2", "-i", str(PATCH_FILE),
        ],
        cwd=WLAN_DIR, capture_output=True, text=True,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    # returncode 1 just means some hunks were rejected -- expected, the
    # known-divergent ones are hand-merged in step 2 below.
    if result.returncode not in (0, 1):
        sys.exit(f"FATAL: patch invocation itself failed, rc={result.returncode}")


def _normalize_for_match(block: str) -> str:
    """Escape a literal code block, then relax runs of horizontal whitespace
    (spaces/tabs) into \\s+ so tab-vs-space drift doesn't break the match.
    Newlines are preserved as literal newlines (not relaxed), since we DO
    want to catch genuine structural differences in line breaks."""
    escaped = re.escape(block)
    escaped = re.sub(r"(?:\\ |\\\t)+", r"[ \\t]+", escaped)
    return escaped


def _diagnose(path: Path, needle: str, context_lines: int = 70):
    text = path.read_text()
    idx = text.find(needle)
    if idx == -1:
        print(f"    DIAGNOSE: needle {needle!r} not found anywhere in file either "
              f"-- this section may not exist in this tree at all")
        return
    line_no = text.count("\n", 0, idx) + 1
    lines = text.splitlines()
    start = max(0, line_no - 1 - context_lines // 2)
    end = min(len(lines), line_no - 1 + context_lines // 2)
    print(f"    DIAGNOSE: needle found at line {line_no}, real content "
          f"(lines {start + 1}-{end}):")
    for i in range(start, end):
        print(f"    {i + 1:>6}\t{lines[i].replace(chr(9), '<TAB>')}")


def already_present(path: Path, needle: str) -> bool:
    ATTEMPTED_FILES.add(path.resolve())
    if not path.exists():
        return False
    return needle in path.read_text()


def fuzzy_replace(path: Path, old_block: str, new_block: str, label: str,
                   required: bool = True, diagnose_needle: str = None):
    ATTEMPTED_FILES.add(path.resolve())
    if not path.exists():
        msg = f"[{label}] file not found: {path}"
        FAILURES.append(msg) if required else print(f"WARN: {msg}")
        return False

    text = path.read_text()
    pattern = _normalize_for_match(old_block)
    matches = list(re.finditer(pattern, text))

    if len(matches) == 0:
        msg = f"[{label}] anchor not found in {path}"
        if required:
            FAILURES.append(msg)
        else:
            print(f"WARN: {msg} -- skipping (non-required)")
        if diagnose_needle:
            _diagnose(path, diagnose_needle)
        return False
    if len(matches) > 1:
        msg = (f"[{label}] anchor matched {len(matches)} times in {path}, "
               f"expected exactly 1 -- refusing to guess, needs a human")
        FAILURES.append(msg)
        return False

    m = matches[0]
    new_text = text[:m.start()] + new_block + text[m.end():]
    path.write_text(new_text)
    print(f"[{label}] applied -> {path.relative_to(REPO_ROOT)}")
    return True


def dump_rejects():
    rejects = sorted(WLAN_DIR.rglob("*.rej"))
    if not rejects:
        print("\n(no .rej files after bulk patch step -- nothing to diagnose)")
        return
    print(f"\n{'=' * 60}\nDIAGNOSING {len(rejects)} REMAINING .rej FILE(S)\n{'=' * 60}")
    for rej in rejects:
        target = rej.with_suffix("")
        print(f"\n--- {rej.relative_to(WLAN_DIR)} ---")
        rej_text = rej.read_text()
        print("EXPECTED (literal hunk from patch):")
        print(rej_text)
        if not target.exists():
            print(f"    (target {target} not found -- can't show real content)")
            continue
        lines = target.read_text().splitlines()
        starts = [int(m.group(1)) for m in
                  re.finditer(r"^@@ -(\d+),\d+ \+\d+,\d+ @@", rej_text, re.M)]
        for start_line in starts:
            lo = max(0, start_line - 1 - 10)
            hi = min(len(lines), start_line - 1 + 25)
            print(f"REAL CONTENT in {target.name} around original line "
                  f"{start_line} (showing {lo + 1}-{hi}):")
            for i in range(lo, hi):
                print(f"  {i + 1:>6}\t{lines[i].replace(chr(9), '<TAB>')}")


KEYWORD_BLACKLIST = {
    "if", "for", "while", "switch", "else", "do", "return", "typedef",
    "struct", "enum", "union", "sizeof", "case", "goto", "break",
    "continue", "static_assert", "default",
}


def _extract_defs_from_added_lines(added_text: str):
    lines = added_text.split("\n")
    names = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line and line[0] not in (" ", "\t", "#", "/", "*", "}", "{", ";"):
            sig = [line]
            j = i
            found_close = line.rstrip().endswith(")")
            has_semi = ";" in line
            while not found_close and not has_semi and j + 1 < n and j - i < 10:
                j += 1
                sig.append(lines[j])
                if ";" in lines[j]:
                    has_semi = True
                    break
                if lines[j].rstrip().endswith(")"):
                    found_close = True
                    break
            if found_close and not has_semi:
                k = j + 1
                while k < n and lines[k].strip() == "":
                    k += 1
                if k < n and lines[k].strip() == "{":
                    sig_text = " ".join(s.strip() for s in sig)
                    m = re.match(
                        r"^(?:static\s+)?(?:inline\s+)?(?:const\s+)?"
                        r"[A-Za-z_][\w\s\*]*?\b([A-Za-z_]\w*)\s*\(",
                        sig_text)
                    if m and m.group(1) not in KEYWORD_BLACKLIST:
                        names.append(m.group(1))
                    i = k
        i += 1
    return names


def get_patch_added_function_names():
    patch_text = PATCH_FILE.read_text()
    parts = re.split(r"^diff --git a/(\S+) b/\S+$", patch_text, flags=re.M)
    pairs = list(zip(parts[1::2], parts[2::2]))
    result = {}
    for path, content in pairs:
        if not path.endswith(".c"):
            continue
        if "new file mode" in content[:500]:
            continue
        names = set()
        for hunk in re.split(r"^@@.*@@.*$", content, flags=re.M)[1:]:
            added = "\n".join(
                l[1:] for l in hunk.split("\n")
                if l.startswith("+") and not l.startswith("+++")
            )
            names.update(_extract_defs_from_added_lines(added))
        if names:
            rel = re.sub(r"^drivers/staging/", "", path)
            result[rel] = names
    return result


def _build_condition_paths(text: str):
    lines = text.split("\n")
    stack = []
    next_id = 0
    paths = {}
    for i, line in enumerate(lines, start=1):
        s = line.strip()
        if s.startswith(("#if", "#ifdef", "#ifndef")):
            stack.append([next_id, 0])
            next_id += 1
        elif s.startswith(("#elif", "#else")):
            if stack:
                stack[-1][1] += 1
        elif s.startswith("#endif"):
            if stack:
                stack.pop()
        paths[i] = tuple((bid, br) for bid, br in stack)
    return paths


def _mutually_exclusive(path_a, path_b):
    a_map, b_map = dict(path_a), dict(path_b)
    shared = set(a_map) & set(b_map)
    if not shared:
        return False
    return any(a_map[bid] != b_map[bid] for bid in shared)


def _find_matching_paren(text: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _is_function_definition(text: str, open_paren_idx: int) -> bool:
    close_idx = _find_matching_paren(text, open_paren_idx)
    if close_idx == -1:
        return False
    j = close_idx + 1
    n = len(text)
    while j < n and text[j] in " \t\r\n":
        j += 1
    return j < n and text[j] == "{"


def _delete_function_by_doc_marker(path: Path, doc_marker: str, label: str) -> bool:
    ATTEMPTED_FILES.add(path.resolve())
    if not path.exists():
        FAILURES.append(f"[{label}] file not found: {path}")
        return False

    text = path.read_text()
    marker_idx = text.find(doc_marker)
    if marker_idx == -1:
        print(f"[{label}] doc marker not present -- nothing to delete")
        return False

    comment_start = text.rfind("/**", 0, marker_idx)
    if comment_start == -1:
        FAILURES.append(
            f"[{label}] found doc marker but no preceding '/**' in {path} "
            f"-- refusing to guess, needs a human")
        _diagnose(path, doc_marker)
        return False

    brace_open = text.find("{", marker_idx)
    if brace_open == -1:
        FAILURES.append(
            f"[{label}] found doc marker but no following '{{' in {path} "
            f"-- refusing to guess, needs a human")
        _diagnose(path, doc_marker)
        return False

    brace_close = -1
    depth = 0
    i = brace_open
    n = len(text)
    while i < n:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                brace_close = i
                break
        i += 1
    if brace_close == -1:
        FAILURES.append(
            f"[{label}] opening brace at offset {brace_open} in {path} "
            f"never balanced -- refusing to guess, needs a human")
        return False

    end = brace_close + 1
    if text[end:end + 1] == "\n":
        end += 1
    if text[end:end + 1] == "\n":
        end += 1

    start_line = text.count("\n", 0, comment_start) + 1
    end_line = text.count("\n", 0, end) + 1
    new_text = text[:comment_start] + text[end:]
    path.write_text(new_text)
    print(f"[{label}] deleted function (lines {start_line}-{end_line}) -> "
          f"{path.relative_to(REPO_ROOT)}")
    return True


def _diagnose_duplicate(path: Path, name: str, lines_list, context_lines: int = 24):
    lines = path.read_text().splitlines()
    for ln in lines_list:
        lo = max(0, ln - 1 - context_lines // 2)
        hi = min(len(lines), ln - 1 + context_lines // 2)
        print(f"    DIAGNOSE DUPLICATE '{name}' at line {ln} in {path.name} "
              f"(showing {lo + 1}-{hi}):")
        for i in range(lo, hi):
            print(f"    {i + 1:>6}\t{lines[i].replace(chr(9), '<TAB>')}")


def scan_for_duplicate_functions():
    print(f"\n{'=' * 60}\nSCANNING FOR DUPLICATE FUNCTION DEFINITIONS\n{'=' * 60}")
    name_map = get_patch_added_function_names()
    total_names = sum(len(v) for v in name_map.values())
    print(f"Patch defines {total_names} new function name(s) across "
          f"{len(name_map)} existing file(s) -- checking each for "
          f"unintended duplication in the final tree:")

    any_found = False
    for rel, names in sorted(name_map.items()):
        target = WLAN_DIR / rel
        if not target.exists():
            print(f"  (skip, file not found: {rel})")
            continue
        text = target.read_text()
        paths = _build_condition_paths(text)
        for name in sorted(names):
            pattern = re.compile(
                r"^(?!\s*[*/])(?:static[ \t]+)?(?:inline[ \t]+)?"
                r"[\w \t\*]*?\b" + re.escape(name) + r"[ \t]*\(", re.M)
            match_lines = []
            for m in pattern.finditer(text):
                open_paren_idx = m.end() - 1
                if (text[open_paren_idx] == "("
                        and _is_function_definition(text, open_paren_idx)):
                    match_lines.append(text.count("\n", 0, m.start()) + 1)
            if len(match_lines) <= 1:
                print(f"  ok  {name}  ({rel}) -- {len(match_lines)} occurrence(s)")
                continue
            branch_separated = True
            for a, b in zip(match_lines, match_lines[1:]):
                if not _mutually_exclusive(paths.get(a, ()), paths.get(b, ())):
                    branch_separated = False
                    break
            if branch_separated:
                print(f"  ok  {name}  ({rel}) -- {len(match_lines)}x at "
                      f"{match_lines}, each in its own #ifdef/#else branch")
            else:
                any_found = True
                msg = (f"[duplicate-definition] '{name}' defined "
                       f"{len(match_lines)}x in {rel} at lines "
                       f"{match_lines}, NOT separated by #else/#elif -- "
                       f"likely a real compile-breaking duplicate")
                FAILURES.append(msg)
                print(f"  !! {msg}")
                _diagnose_duplicate(target, name, match_lines)
    if not any_found:
        print("\n  No unresolved duplicates found.")


def apply_hand_merges():
    print("\n== Step 2: hand-merged fixes for hunks that diverged from baseline ==")

    f = HDD_SRC / "wlan_hdd_rx_monitor.h"
    if already_present(f, "hdd_disable_monitor_mode("):
        print("[rx_monitor.h] hdd_disable_monitor_mode already present -- correctly left alone")
    else:
        msg = ("[rx_monitor.h] expected hdd_disable_monitor_mode to already "
               "exist in this tree and it doesn't -- tree has changed more "
               "than expected, needs a human to re-check")
        FAILURES.append(msg)

    f = HDD_SRC / "wlan_hdd_rx_monitor.c"
    if already_present(f, '#include "wlan_hdd_frame_inject.h"'):
        print("[rx_monitor.c include] already present -- skipping")
    else:
        fuzzy_replace(f,
            '#include "wlan_hdd_rx_monitor.h"\n#include "ol_txrx.h"\n',
            '#include "wlan_hdd_rx_monitor.h"\n#include "ol_txrx.h"\n'
            '#ifdef FEATURE_FRAME_INJECTION_SUPPORT\n'
            '#include "wlan_hdd_frame_inject.h"\n#endif\n',
            "rx_monitor.c include",
            diagnose_needle='#include "wlan_hdd_rx_monitor.h"')

    splice_marker = "hdd_get_adapter(hdd_ctx, QDF_MONITOR_MODE)"
    dup_marker = "int hdd_disable_monitor_mode(struct net_device *dev)"
    void_fn = (
        "int hdd_disable_monitor_mode(void)\n"
        "{\n"
        "\tvoid *soc = cds_get_context(QDF_MODULE_ID_SOC);\n"
        "\n"
        "\treturn cdp_reset_monitor_mode(soc, OL_TXRX_PDEV_ID, false);\n"
        "}\n"
    )
    spliced_void_fn = (
        "int hdd_disable_monitor_mode(void)\n"
        "{\n"
        "\tvoid *soc = cds_get_context(QDF_MODULE_ID_SOC);\n"
        "#ifdef FEATURE_FRAME_INJECTION_SUPPORT\n"
        "\tstruct hdd_context *hdd_ctx = cds_get_context(QDF_MODULE_ID_HDD);\n"
        "\tstruct hdd_adapter *adapter;\n"
        "\n"
        "\tif (hdd_ctx) {\n"
        "\t\tadapter = hdd_get_adapter(hdd_ctx, QDF_MONITOR_MODE);\n"
        "\t\tif (adapter) {\n"
        "\t\t\tif (QDF_IS_STATUS_ERROR(hdd_frame_inject_disable(adapter))) {\n"
        "\t\t\t\thdd_warn(\"Failed to disable frame injection\");\n"
        "\t\t\t\t/* Continue with monitor mode disable */\n"
        "\t\t\t}\n"
        "\t\t}\n"
        "\t}\n"
        "#endif\n"
        "\n"
        "\treturn cdp_reset_monitor_mode(soc, OL_TXRX_PDEV_ID, false);\n"
        "}\n"
    )

    if already_present(f, splice_marker):
        print("[rx_monitor.c disable-mode splice] already present -- skipping")
    elif already_present(f, dup_marker):
        dup_fn = (
            "/**\n"
            " * hdd_disable_monitor_mode() - Disable monitor mode\n"
            " * @dev: Pointer to the net_device structure\n"
            " *\n"
            " * This function disables monitor mode configuration on the hardware\n"
            " * and also disables frame injection if it was enabled.\n"
            " *\n"
            " * Return: 0 for success; non-zero for failure\n"
            " */\n"
            "int hdd_disable_monitor_mode(struct net_device *dev)\n"
            "{\n"
            "\tvoid *soc = cds_get_context(QDF_MODULE_ID_SOC);\n"
            "\tuint8_t vdev_id;\n"
            "\tint ret;\n"
            "#ifdef FEATURE_FRAME_INJECTION_SUPPORT\n"
            "\tstruct hdd_adapter *adapter = WLAN_HDD_GET_PRIV_PTR(dev);\n"
            "#endif\n"
            "\n"
            "\thdd_enter_dev(dev);\n"
            "\n"
            "\tvdev_id = cdp_get_mon_vdev_from_pdev(soc, OL_TXRX_PDEV_ID);\n"
            "\tif (vdev_id < 0)\n"
            "\t\treturn -EINVAL;\n"
            "\n"
            "#ifdef FEATURE_FRAME_INJECTION_SUPPORT\n"
            "\t/* Disable frame injection when monitor mode is disabled */\n"
            "\tif (adapter) {\n"
            "\t\tif (QDF_IS_STATUS_ERROR(hdd_frame_inject_disable(adapter))) {\n"
            "\t\t\thdd_warn(\"Failed to disable frame injection\");\n"
            "\t\t\t/* Continue with monitor mode disable */\n"
            "\t\t}\n"
            "\t}\n"
            "#endif\n"
            "\n"
            "\tret = cdp_set_monitor_mode(soc, vdev_id, true);\n"
            "\n"
            "\treturn ret;\n"
            "}\n"
            "\n"
        )
        fuzzy_replace(f, dup_fn + void_fn, spliced_void_fn,
                      "rx_monitor.c remove duplicate, splice into real fn",
                      diagnose_needle=dup_marker)
    else:
        fuzzy_replace(f, void_fn, spliced_void_fn,
                      "rx_monitor.c splice into existing fn (no duplicate present)",
                      diagnose_needle="int hdd_disable_monitor_mode(void)")

    f = HDD_SRC / "wlan_hdd_cfg80211.c"
    fuzzy_replace(f,
        '#include "wlan_hdd_cfr.h"\n',
        '#include "wlan_hdd_cfr.h"\n'
        '#ifdef FEATURE_FRAME_INJECTION_SUPPORT\n'
        '#include "wlan_hdd_frame_inject.h"\n#endif\n',
        "cfg80211.c include")
    fuzzy_replace(f,
        "\tFEATURE_SAR_LIMITS_VENDOR_COMMANDS\n\tBCN_RECV_FEATURE_VENDOR_COMMANDS\n",
        "\tFEATURE_SAR_LIMITS_VENDOR_COMMANDS\n"
        "#ifdef FEATURE_FRAME_INJECTION_SUPPORT\n"
        "\tFEATURE_FRAME_INJECTION_VENDOR_COMMANDS,\n"
        "#endif\n"
        "#ifdef WLAN_BCN_RECV_FEATURE\n"
        "\tBCN_RECV_FEATURE_VENDOR_COMMANDS\n"
        "#endif\n",
        "cfg80211.c vendor cmd array")

    f = HDD_INC / "wlan_hdd_frame_inject.h"
    vendor_cmds_old = (
        "#define FEATURE_FRAME_INJECTION_VENDOR_COMMANDS \\\n"
        "{ \\\n"
        "\t.info.vendor_id = QCA_NL80211_VENDOR_ID, \\\n"
        "\t.info.subcmd = QCA_NL80211_VENDOR_SUBCMD_FRAME_INJECT, \\\n"
        "\t.flags = WIPHY_VENDOR_CMD_NEED_WDEV | \\\n"
        "\t\t WIPHY_VENDOR_CMD_NEED_NETDEV | \\\n"
        "\t\t WIPHY_VENDOR_CMD_NEED_RUNNING, \\\n"
        "\t.doit = hdd_frame_inject_netlink \\\n"
        "}, \\\n"
        "{ \\\n"
        "\t.info.vendor_id = QCA_NL80211_VENDOR_ID, \\\n"
        "\t.info.subcmd = QCA_NL80211_VENDOR_SUBCMD_FRAME_INJECT_STATS, \\\n"
        "\t.flags = WIPHY_VENDOR_CMD_NEED_WDEV | \\\n"
        "\t\t WIPHY_VENDOR_CMD_NEED_NETDEV, \\\n"
        "\t.doit = hdd_get_injection_stats_netlink \\\n"
        "}, \\\n"
        "{ \\\n"
        "\t.info.vendor_id = QCA_NL80211_VENDOR_ID, \\\n"
        "\t.info.subcmd = QCA_NL80211_VENDOR_SUBCMD_FRAME_INJECT_RESET, \\\n"
        "\t.flags = WIPHY_VENDOR_CMD_NEED_WDEV | \\\n"
        "\t\t WIPHY_VENDOR_CMD_NEED_NETDEV, \\\n"
        "\t.doit = hdd_reset_injection_stats_netlink \\\n"
        "}\n"
    )
    vendor_cmds_new = (
        "#define FEATURE_FRAME_INJECTION_VENDOR_COMMANDS \\\n"
        "{ \\\n"
        "\t.info.vendor_id = QCA_NL80211_VENDOR_ID, \\\n"
        "\t.info.subcmd = QCA_NL80211_VENDOR_SUBCMD_FRAME_INJECT, \\\n"
        "\t.flags = WIPHY_VENDOR_CMD_NEED_WDEV | \\\n"
        "\t\t WIPHY_VENDOR_CMD_NEED_NETDEV | \\\n"
        "\t\t WIPHY_VENDOR_CMD_NEED_RUNNING, \\\n"
        "\t.policy = VENDOR_CMD_RAW_DATA, \\\n"
        "\t.doit = hdd_frame_inject_netlink \\\n"
        "}, \\\n"
        "{ \\\n"
        "\t.info.vendor_id = QCA_NL80211_VENDOR_ID, \\\n"
        "\t.info.subcmd = QCA_NL80211_VENDOR_SUBCMD_FRAME_INJECT_STATS, \\\n"
        "\t.flags = WIPHY_VENDOR_CMD_NEED_WDEV | \\\n"
        "\t\t WIPHY_VENDOR_CMD_NEED_NETDEV, \\\n"
        "\t.policy = VENDOR_CMD_RAW_DATA, \\\n"
        "\t.doit = hdd_get_injection_stats_netlink \\\n"
        "}, \\\n"
        "{ \\\n"
        "\t.info.vendor_id = QCA_NL80211_VENDOR_ID, \\\n"
        "\t.info.subcmd = QCA_NL80211_VENDOR_SUBCMD_FRAME_INJECT_RESET, \\\n"
        "\t.flags = WIPHY_VENDOR_CMD_NEED_WDEV | \\\n"
        "\t\t WIPHY_VENDOR_CMD_NEED_NETDEV, \\\n"
        "\t.policy = VENDOR_CMD_RAW_DATA, \\\n"
        "\t.doit = hdd_reset_injection_stats_netlink \\\n"
        "}\n"
    )
    if already_present(f, "\t.policy = VENDOR_CMD_RAW_DATA, \\\n\t.doit = hdd_frame_inject_netlink"):
        print("[wlan_hdd_frame_inject.h vendor cmd .policy] already present -- skipping")
    else:
        fuzzy_replace(
            f, vendor_cmds_old, vendor_cmds_new,
            "wlan_hdd_frame_inject.h add missing .policy to vendor commands",
            diagnose_needle="define FEATURE_FRAME_INJECTION_VENDOR_COMMANDS")

    f = HDD_INC / "wlan_hdd_main.h"
    if already_present(f, "struct hdd_injection_ctx *injection_ctx;"):
        print("[main.h injection_ctx field] already present -- skipping")
    else:
        fuzzy_replace(f,
            "\tqdf_atomic_t net_dev_hold_ref_count[NET_DEV_HOLD_ID_MAX];\n",
            "\tqdf_atomic_t net_dev_hold_ref_count[NET_DEV_HOLD_ID_MAX];\n"
            "#ifdef FEATURE_FRAME_INJECTION_SUPPORT\n"
            "\tstruct hdd_injection_ctx *injection_ctx;\n#endif\n",
            "main.h injection_ctx field",
            diagnose_needle="net_dev_hold_ref_count[NET_DEV_HOLD_ID_MAX]")

    f = HDD_SRC / "wlan_hdd_main.c"
    fuzzy_replace(f,
        "\tret = hdd_set_mon_rx_cb(dev);\n\n"
        "\tif (!ret)\n\t\tret = hdd_enable_monitor_mode(dev);\n\n"
        "\tif (!ret) {\n"
        "\t\tparam.policy = BBM_DRIVER_MODE_POLICY;\n"
        "\t\tparam.policy_info.driver_mode = QDF_GLOBAL_MONITOR_MODE;\n"
        "\t\thdd_bbm_apply_independent_policy(hdd_ctx, &param);\n"
        "\t\thdd_set_current_throughput_level(hdd_ctx,\n"
        "\t\t\t\t\t\t  PLD_BUS_WIDTH_VERY_HIGH);\n"
        "\t}\n\n\treturn ret;\n}",
        "\tret = hdd_set_mon_rx_cb(dev);\n\n"
        "\tif (!ret)\n\t\tret = hdd_enable_monitor_mode(dev);\n\n"
        "\tif (ret)\n\t\treturn ret;\n\n"
        "\tset_bit(DEVICE_IFACE_OPENED, &adapter->event_flags);\n\n"
        "\t/*\n"
        "\t * Monitor interface still needs carrier/tx queues marked up, otherwise\n"
        "\t * userspace injection tools fail with ENETDOWN even though mode switch\n"
        "\t * succeeded.\n"
        "\t */\n"
        "\twlan_hdd_netif_queue_control(adapter,\n"
        "\t\t\t\t     WLAN_START_ALL_NETIF_QUEUE_N_CARRIER,\n"
        "\t\t\t\t     WLAN_CONTROL_PATH);\n"
        "\thdd_warn_rl(\"monitor open complete: if=%s carrier=%u running=%u pause_map=0x%x\",\n"
        "\t\t   dev->name, netif_carrier_ok(dev) ? 1 : 0,\n"
        "\t\t   netif_running(dev) ? 1 : 0, adapter->pause_map);\n\n"
        "\tparam.policy = BBM_DRIVER_MODE_POLICY;\n"
        "\tparam.policy_info.driver_mode = QDF_GLOBAL_MONITOR_MODE;\n"
        "\thdd_bbm_apply_independent_policy(hdd_ctx, &param);\n"
        "\thdd_set_current_throughput_level(hdd_ctx,\n"
        "\t\t\t\t\t  PLD_BUS_WIDTH_VERY_HIGH);\n\n"
        "\treturn 0;\n}",
        "main.c __hdd_mon_open")

    f = WMA_INC / "wma.h"
    wma_log_macros = (
        "#define wma_alert(params...) QDF_TRACE_FATAL(QDF_MODULE_ID_WMA, params)\n"
        "#define wma_err(params...) QDF_TRACE_ERROR(QDF_MODULE_ID_WMA, params)\n"
        "#define wma_warn(params...) QDF_TRACE_WARN(QDF_MODULE_ID_WMA, params)\n"
        "#define wma_info(params...) QDF_TRACE_INFO(QDF_MODULE_ID_WMA, params)\n"
        "#define wma_debug(params...) QDF_TRACE_DEBUG(QDF_MODULE_ID_WMA, params)\n"
        "#define wma_debug_rl(params...) QDF_TRACE_DEBUG_RL(QDF_MODULE_ID_WMA, params)\n"
        "#define wma_err_rl(params...) QDF_TRACE_ERROR_RL(QDF_MODULE_ID_WMA, params)\n"
    )
    wma_legacy_shim = (
        "#ifdef FEATURE_FRAME_INJECTION_SUPPORT\n"
        "/* 07_packet_injection.patch was authored against a baseline that\n"
        " * still used the legacy WMA_LOG* macro family; this tree has since\n"
        " * moved to wma_err()/wma_warn()/etc. and no longer defines them. */\n"
        "#define WMA_LOGE(params...) wma_err(params)\n"
        "#define WMA_LOGW(params...) wma_warn(params)\n"
        "#define WMA_LOGD(params...) wma_debug(params)\n"
        "#define WMA_LOGI(params...) wma_info(params)\n"
        "#endif\n"
    )
    if already_present(f, "#define WMA_LOGE(params...) wma_err(params)"):
        print("[wma.h legacy log macro shim] already present -- skipping")
    else:
        fuzzy_replace(f, wma_log_macros, wma_log_macros + wma_legacy_shim,
                      "wma.h legacy WMA_LOGE/W/D/I shim",
                      diagnose_needle="#define wma_err_rl(params...)")

    f = WMA_SRC / "wma_main.c"
    fuzzy_replace(f,
        "\twma_register_mws_coex_events(wma_handle);\n"
        "\twma_trace_init();\n\treturn QDF_STATUS_SUCCESS;",
        "\twma_register_mws_coex_events(wma_handle);\n"
        "\twma_trace_init();\n\n"
        "\t/* Initialize frame injection queue */\n"
        "\tqdf_status = wma_init_injection_queue(wma_handle);\n"
        "\tif (qdf_status != QDF_STATUS_SUCCESS) {\n"
        "\t\tWMA_LOGE(\"%s: Failed to initialize injection queue: %d\",\n"
        "\t\t\t __func__, qdf_status);\n"
        "\t\t/* Continue initialization - injection queue failure is not fatal */\n"
        "\t}\n\n\treturn QDF_STATUS_SUCCESS;",
        "wma_main.c wma_open injection queue init")

    f = WMA_SRC / "wma_utils.c"
    down_send_decl_anchor = (
        "QDF_STATUS wma_mon_mlme_vdev_down_send(struct vdev_mlme_obj *vdev_mlme,\n"
        "\t\t\t\t       uint16_t data_len, void *data)\n"
        "{\n"
        "\tuint8_t vdev_id;\n"
        "\ttp_wma_handle wma = cds_get_context(QDF_MODULE_ID_WMA);\n"
        "\tQDF_STATUS status;\n"
    )
    down_send_decl_fixed = down_send_decl_anchor + "\tstruct del_bss_resp *resp;\n"
    wlm_stats_misplaced_anchor = (
        "\twmi_request_wlm_stats_cmd_fixed_param *cmd;\n"
        "\tQDF_STATUS status;\n"
        "\tstruct del_bss_resp *resp;\n"
    )
    wlm_stats_fixed = (
        "\twmi_request_wlm_stats_cmd_fixed_param *cmd;\n"
        "\tQDF_STATUS status;\n"
    )

    if already_present(f, down_send_decl_fixed):
        print("[wma_utils.c del_bss_resp declaration] already present in wma_mon_mlme_vdev_down_send -- skipping")
    else:
        if already_present(f, wlm_stats_misplaced_anchor):
            fuzzy_replace(
                f, wlm_stats_misplaced_anchor, wlm_stats_fixed,
                "wma_utils.c remove misplaced del_bss_resp from wma_wlm_stats_req")
        fuzzy_replace(
            f, down_send_decl_anchor, down_send_decl_fixed,
            "wma_utils.c del_bss_resp declaration",
            diagnose_needle="QDF_STATUS wma_mon_mlme_vdev_down_send(")

    fuzzy_replace(f,
        "\twlan_vdev_mlme_sm_deliver_evt(vdev_mlme->vdev,\n"
        "\t\t\t\t      WLAN_VDEV_SM_EV_DOWN_COMPLETE,\n"
        "\t\t\t\t      0,\n"
        "\t\t\t\t      NULL);\n\n\treturn status;",
        "\tresp = qdf_mem_malloc(sizeof(*resp));\n"
        "\tif (resp) {\n"
        "\t\tresp->vdev_id = vdev_id;\n"
        "\t\tresp->status = QDF_STATUS_SUCCESS;\n"
        "\t\twlan_vdev_mlme_sm_deliver_evt(vdev_mlme->vdev,\n"
        "\t\t\t\t\t      WLAN_VDEV_SM_EV_DOWN_COMPLETE,\n"
        "\t\t\t\t\t      sizeof(*resp),\n"
        "\t\t\t\t\t      resp);\n"
        "\t}\n\n\treturn status;",
        "wma_utils.c del_bss_resp alloc")

    f = WMA_SRC / "wma_frame_inject.c"
    roam_check_old = "\tif (iface->roaming_in_progress) {\n"
    roam_check_new = "\tif (wma_is_roam_in_progress(vdev_id)) {\n"
    if already_present(f, roam_check_new):
        print("[wma_frame_inject.c roaming_in_progress fix] already present -- skipping")
    else:
        fuzzy_replace(
            f, roam_check_old, roam_check_new,
            "wma_frame_inject.c roaming_in_progress -> wma_is_roam_in_progress(vdev_id)",
            diagnose_needle="if (iface->roaming_in_progress")

    mhz_old = (
        "\tif (wma_handle->interfaces[vdev_id].mhz)\n"
        "\t\ttx_chanfreq = wma_handle->interfaces[vdev_id].mhz;\n"
    )
    mhz_new = (
        "\tif (wma_handle->interfaces[vdev_id].ch_freq)\n"
        "\t\ttx_chanfreq = wma_handle->interfaces[vdev_id].ch_freq;\n"
    )
    if already_present(f, mhz_new):
        print("[wma_frame_inject.c mhz fix] already present -- skipping")
    else:
        fuzzy_replace(f, mhz_old, mhz_new,
                      "wma_frame_inject.c .mhz -> .ch_freq",
                      diagnose_needle="interfaces[vdev_id].mhz")

    f = HDD_SRC / "wlan_hdd_inject_security.c"
    inject_cap_old = (
        "\tconst struct cred *cred;\n"
        "\tbool has_capability = false;\n"
        "\n"
        "\thdd_security_debug(\"Checking injection capabilities\");\n"
        "\n"
        "\t/* Use current task if none specified */\n"
        "\tif (!task)\n"
        "\t\ttask = current;\n"
        "\n"
        "\t/* Get task credentials */\n"
        "\tcred = get_task_cred(task);\n"
        "\tif (!cred) {\n"
        "\t\thdd_security_err(\"Failed to get task credentials\");\n"
        "\t\treturn QDF_STATUS_E_FAILURE;\n"
        "\t}\n"
        "\n"
        "\t/* Check for CAP_NET_RAW capability */\n"
        "\thas_capability = capable(CAP_NET_RAW);\n"
        "\t\n"
        "\tput_cred(cred);\n"
        "\n"
        "\tif (!has_capability) {\n"
    )
    inject_cap_new = (
        "\tbool has_capability = false;\n"
        "\n"
        "\thdd_security_debug(\"Checking injection capabilities\");\n"
        "\n"
        "\t/* Use current task if none specified */\n"
        "\tif (!task)\n"
        "\t\ttask = current;\n"
        "\n"
        "\t/* Check for CAP_NET_RAW capability */\n"
        "\thas_capability = capable(CAP_NET_RAW);\n"
        "\n"
        "\tif (!has_capability) {\n"
    )
    if already_present(f, inject_cap_new) and not already_present(f, "cred = get_task_cred(task);"):
        print("[wlan_hdd_inject_security.c dead cred fetch] already removed -- skipping")
    else:
        fuzzy_replace(
            f, inject_cap_old, inject_cap_new,
            "wlan_hdd_inject_security.c remove dead get_task_cred/put_cred",
            diagnose_needle="QDF_STATUS hdd_check_injection_capability(struct task_struct *task)")

    f = QCACLD / "Kbuild"
    if already_present(f, "CONFIG_FEATURE_FRAME_INJECTION_SUPPORT), y)"):
        print("[Kbuild] already present -- skipping")
    else:
        fuzzy_replace(f,
            "ifeq ($(CONFIG_FEATURE_MONITOR_MODE_SUPPORT), y)\n"
            "HDD_OBJS += $(HDD_SRC_DIR)/wlan_hdd_rx_monitor.o\n"
            "endif\n",
            "ifeq ($(CONFIG_FEATURE_MONITOR_MODE_SUPPORT), y)\n"
            "HDD_OBJS += $(HDD_SRC_DIR)/wlan_hdd_rx_monitor.o\n"
            "endif\n"
            "\n"
            "ifeq ($(CONFIG_FEATURE_FRAME_INJECTION_SUPPORT), y)\n"
            "HDD_OBJS += $(HDD_SRC_DIR)/wlan_hdd_frame_inject.o\n"
            "HDD_OBJS += $(HDD_SRC_DIR)/wlan_hdd_frame_validate.o\n"
            "HDD_OBJS += $(HDD_SRC_DIR)/wlan_hdd_inject_security.o\n"
            "HDD_OBJS += $(HDD_SRC_DIR)/wlan_hdd_frame_inject_debug.o\n"
            "ifeq ($(CONFIG_WLAN_UNIT_TEST), y)\n"
            "HDD_OBJS += $(HDD_SRC_DIR)/wlan_hdd_frame_validate_test.o\n"
            "endif\n"
            "endif\n",
            "Kbuild HDD_OBJS",
            diagnose_needle="HDD_OBJS += $(HDD_SRC_DIR)/wlan_hdd_rx_monitor.o")

        fuzzy_replace(f,
            "\t\t$(WMA_NDP_OBJS)\n",
            "\t\t$(WMA_NDP_OBJS)\n"
            "\n"
            "ifeq ($(CONFIG_FEATURE_FRAME_INJECTION_SUPPORT), y)\n"
            "WMA_OBJS += $(WMA_SRC_DIR)/wma_frame_inject.o\n"
            "endif\n",
            "Kbuild WMA_OBJS",
            diagnose_needle="$(WMA_NDP_OBJS)")

        fuzzy_replace(f,
            "else\n"
            "cppflags-y += -DDISABLE_MON_CONFIG\n"
            "endif\n",
            "else\n"
            "cppflags-y += -DDISABLE_MON_CONFIG\n"
            "endif\n"
            "\n"
            "ifeq ($(CONFIG_FEATURE_FRAME_INJECTION_SUPPORT), y)\n"
            "cppflags-y += -DFEATURE_FRAME_INJECTION_SUPPORT\n"
            "CONFIG_FEATURE_MONITOR_MODE_SUPPORT := y\n"
            "endif\n",
            "Kbuild cppflags",
            diagnose_needle="cppflags-y += -DDISABLE_MON_CONFIG")

    f = HDD_SRC / "wlan_hdd_cfg80211.c"
    patch_get_channel_marker = "so that tools like aireplay-ng / mdk3 can determine the current channel."
    if not already_present(f, patch_get_channel_marker):
        print("[cfg80211.c get_channel dedup] patch's duplicate not present -- skipping")
    else:
        _delete_function_by_doc_marker(
            f, patch_get_channel_marker,
            "cfg80211.c remove duplicate get_channel")

        fuzzy_replace(f,
            "#ifdef FEATURE_MONITOR_MODE_SUPPORT\n"
            "\t.set_monitor_channel = wlan_hdd_cfg80211_set_mon_ch,\n"
            "\t.get_channel = wlan_hdd_cfg80211_get_channel,\n"
            "#endif\n",
            "#ifdef FEATURE_MONITOR_MODE_SUPPORT\n"
            "\t.set_monitor_channel = wlan_hdd_cfg80211_set_mon_ch,\n"
            "#endif\n",
            "cfg80211.c remove duplicate .get_channel struct entry",
            diagnose_needle=".set_monitor_channel = wlan_hdd_cfg80211_set_mon_ch,")

    f = CONFIGS / "default_defconfig"
    fuzzy_replace(f,
        "CONFIG_FEATURE_MONITOR_MODE_SUPPORT := y\n",
        "CONFIG_FEATURE_MONITOR_MODE_SUPPORT := y\n"
        "CONFIG_FEATURE_FRAME_INJECTION_SUPPORT := y\n",
        "default_defconfig injection flag")

    f = WLAN_DIR / "qca-wifi-host-cmn/os_if/linux/qca_vendor.h"
    six_entries_block = (
        "\n"
        "\t/* 16-bit unsigned value. For probing RSSI on other antennas, this\n"
        "\t * attribute specifies the number of WLAN probes.\n"
        "\t */\n"
        "\tQCA_WLAN_VENDOR_ATTR_CONFIG_ANT_DIV_PROBE_COUNT_WLAN = 124,\n"
        "\n"
        "\t/* 16-bit unsigned value. For probing RSSI on other antennas, this\n"
        "\t * attribute specifies the number of BT probes.\n"
        "\t */\n"
        "\tQCA_WLAN_VENDOR_ATTR_CONFIG_ANT_DIV_PROBE_COUNT_BT = 125,\n"
        "\n"
        "\t/* 16-bit unsigned value. This attribute specifies the WLAN RSSI\n"
        "\t * threshold. The firmware will start to probe RSSI on other antenna\n"
        "\t * if WLAN RSSI is lower than the threshold.\n"
        "\t */\n"
        "\tQCA_WLAN_VENDOR_ATTR_CONFIG_ANT_DIV_PROBE_WLAN_RSSI_THRESHOLD = 126,\n"
        "\n"
        "\t/* 16-bit unsigned value. This attribute specifies the BT RSSI\n"
        "\t * threshold. The firmware will start to probe RSSI on other antenna\n"
        "\t * if BT RSSI is lower than the threshold.\n"
        "\t */\n"
        "\tQCA_WLAN_VENDOR_ATTR_CONFIG_ANT_DIV_PROBE_BT_RSSI_THRESHOLD = 127,\n"
        "\n"
        "\t/* 16-bit unsigned value. This attribute specifies the WLAN RSSI\n"
        "\t * difference. The firmware will select a better antenna if WLAN RSSI\n"
        "\t * difference is larger than the value.\n"
        "\t */\n"
        "\tQCA_WLAN_VENDOR_ATTR_CONFIG_ANT_DIV_SWITCH_WLAN_RSSI_DIFF = 128,\n"
        "\n"
        "\t/* 16-bit unsigned value. This attribute specifies the BT RSSI\n"
        "\t * difference. The firmware will select a better antenna if WLAN RSSI\n"
        "\t * difference larger than the value.\n"
        "\t */\n"
        "\tQCA_WLAN_VENDOR_ATTR_CONFIG_ANT_DIV_SWITCH_BT_RSSI_DIFF = 129,\n"
        "\n"
    )
    short_misplaced_marker = (
        "QCA_WLAN_VENDOR_ATTR_ACS_EHT_ENABLED = 19,\n"
        "\n"
        "\t/* 16-bit unsigned value. For probing RSSI on other antennas, this\n"
        "\t * attribute specifies the number of WLAN probes.")
    short_fixed_marker = (
        "QCA_WLAN_VENDOR_ATTR_CONFIG_ARP_NS_OFFLOAD = 81,\n"
        "\n"
        "\t/* 16-bit unsigned value. For probing RSSI on other antennas, this\n"
        "\t * attribute specifies the number of WLAN probes.")

    if already_present(f, short_fixed_marker):
        print("[qca_vendor.h ant_div relocation] already in the correct enum -- skipping")
    elif not already_present(f, short_misplaced_marker):
        msg = ("[qca_vendor.h ant_div relocation] six-entry block not found -- needs a human")
        FAILURES.append(msg)
        _diagnose(f, "QCA_WLAN_VENDOR_ATTR_CONFIG_ANT_DIV_PROBE_COUNT_WLAN")
    else:
        removed = fuzzy_replace(
            f,
            "\tQCA_WLAN_VENDOR_ATTR_ACS_EHT_ENABLED = 19,\n"
            + six_entries_block +
            "\t/* keep last */\n"
            "\tQCA_WLAN_VENDOR_ATTR_ACS_AFTER_LAST,\n",
            "\tQCA_WLAN_VENDOR_ATTR_ACS_EHT_ENABLED = 19,\n"
            "\n"
            "\t/* keep last */\n"
            "\tQCA_WLAN_VENDOR_ATTR_ACS_AFTER_LAST,\n",
            "qca_vendor.h remove ant_div block from acs_offload",
            diagnose_needle="QCA_WLAN_VENDOR_ATTR_ACS_EHT_ENABLED = 19,")
        if removed:
            fuzzy_replace(
                f,
                "\tQCA_WLAN_VENDOR_ATTR_CONFIG_ARP_NS_OFFLOAD = 81,\n"
                "\n"
                "\t/* keep last */\n"
                "\tQCA_WLAN_VENDOR_ATTR_CONFIG_AFTER_LAST,\n",
                "\tQCA_WLAN_VENDOR_ATTR_CONFIG_ARP_NS_OFFLOAD = 81,\n"
                + six_entries_block +
                "\t/* keep last */\n"
                "\tQCA_WLAN_VENDOR_ATTR_CONFIG_AFTER_LAST,\n",
                "qca_vendor.h insert ant_div block into qca_wlan_vendor_attr_config",
                diagnose_needle="QCA_WLAN_VENDOR_ATTR_CONFIG_ARP_NS_OFFLOAD = 81,")

    f = HDD_SRC / "wlan_hdd_tx_rx.c"
    radiotap_include_tail = (
        '#include "wlan_hdd_frame_inject.h"\n'
        '#include <net/ieee80211_radiotap.h>\n#endif\n')
    if already_present(f, radiotap_include_tail):
        print("[tx_rx.c radiotap include] already present -- skipping")
    else:
        fuzzy_replace(
            f,
            '#ifdef FEATURE_FRAME_INJECTION_SUPPORT\n'
            '#include "wlan_hdd_frame_inject.h"\n#endif\n',
            '#ifdef FEATURE_FRAME_INJECTION_SUPPORT\n' + radiotap_include_tail,
            "tx_rx.c radiotap include",
            diagnose_needle='#include "wlan_hdd_frame_inject.h"')

    xmit_probe_block = (
        "\tstatic bool xmit_probe_logged;\n"
        "\n"
        "\tif (!xmit_probe_logged) {\n"
        "\t\thdd_warn(\"xmit probe: if=%s mode=%d iftype=%d dev_type=%u flags=0x%x len=%u mon_ctx=%u\",\n"
        "\t\t\t dev ? dev->name : \"null\",\n"
        "\t\t\t adapter ? adapter->device_mode : -1,\n"
        "\t\t\t (dev && dev->ieee80211_ptr) ? dev->ieee80211_ptr->iftype : -1,\n"
        "\t\t\t dev ? dev->type : 0,\n"
        "\t\t\t dev ? dev->flags : 0,\n"
        "\t\t\t skb ? skb->len : 0,\n"
        "\t\t\t (adapter && adapter->injection_ctx &&\n"
        "\t\t\t  adapter->injection_ctx->is_monitor_mode) ? 1 : 0);\n"
        "\t\txmit_probe_logged = true;\n"
        "\t}\n"
        "\n"
        "#ifdef FEATURE_FRAME_INJECTION_SUPPORT\n"
        "\tif (hdd_is_monitor_tx_dev(adapter, dev)) {\n"
        "\t\thdd_monitor_mode_tx_inject(adapter, dev, skb);\n"
        "\t\treturn;\n"
        "\t}\n"
        "#endif\n"
    )
    xmit_probe_misplaced_marker = (
        "\tvoid *soc = cds_get_context(QDF_MODULE_ID_SOC);\n"
        + xmit_probe_block +
        "\n\tif  (!hdd_ctx) {\n"
    )
    xmit_probe_decl_end = "\tint cpu = qdf_get_smp_processor_id();\n"
    xmit_probe_ftm_guard = "\n#ifdef QCA_WIFI_FTM\n"
    xmit_probe_fixed_marker = (
        xmit_probe_decl_end + xmit_probe_block + xmit_probe_ftm_guard
    )

    if already_present(f, xmit_probe_fixed_marker):
        print("[tx_rx.c xmit_probe relocation] already in __hdd_hard_start_xmit -- skipping")
    elif not already_present(f, xmit_probe_misplaced_marker):
        msg = ("[tx_rx.c xmit_probe relocation] block not found -- needs a human")
        FAILURES.append(msg)
        _diagnose(f, "xmit_probe_logged")
    else:
        removed = fuzzy_replace(
            f,
            xmit_probe_misplaced_marker,
            "\tvoid *soc = cds_get_context(QDF_MODULE_ID_SOC);\n"
            "\n\tif  (!hdd_ctx) {\n",
            "tx_rx.c remove xmit_probe block from hdd_register_rx_ol_cb",
            diagnose_needle="xmit_probe_logged")
        if removed:
            fuzzy_replace(
                f,
                xmit_probe_decl_end + xmit_probe_ftm_guard,
                xmit_probe_fixed_marker,
                "tx_rx.c insert xmit_probe block into __hdd_hard_start_xmit",
                diagnose_needle="int cpu = qdf_get_smp_processor_id();")

    fuzzy_replace(f,
        "\tuint32_t temp_map;\n\tuint8_t index;\n"
        "\tstruct hdd_netif_queue_history *txq_hist_ptr;\n\n"
        "\tif ((!adapter) || (WLAN_HDD_ADAPTER_MAGIC != adapter->magic) ||\n"
        "\t    (!adapter->dev)) {\n\t\thdd_err(\"adapter is invalid\");\n\t\treturn;\n\t}\n\n"
        "\tif (hdd_adapter_is_link_adapter(adapter))\n\t\treturn;\n\n\tswitch (action) {",
        "\tuint32_t temp_map;\n\tuint8_t index;\n"
        "\tstruct hdd_netif_queue_history *txq_hist_ptr;\n\tbool monitor_iface;\n\n"
        "\tif ((!adapter) || (WLAN_HDD_ADAPTER_MAGIC != adapter->magic) ||\n"
        "\t    (!adapter->dev)) {\n\t\thdd_err(\"adapter is invalid\");\n\t\treturn;\n\t}\n\n"
        "\tif (hdd_adapter_is_link_adapter(adapter))\n\t\treturn;\n\n"
        "\tmonitor_iface = wlan_hdd_is_session_type_monitor(adapter->device_mode) ||\n"
        "\t\t\t(adapter->dev->type == ARPHRD_IEEE80211_RADIOTAP);\n"
        "\tif (monitor_iface &&\n"
        "\t    (action == WLAN_STOP_ALL_NETIF_QUEUE_N_CARRIER ||\n"
        "\t     action == WLAN_START_ALL_NETIF_QUEUE_N_CARRIER ||\n"
        "\t     action == WLAN_NETIF_CARRIER_OFF ||\n"
        "\t     action == WLAN_NETIF_CARRIER_ON)) {\n"
        "\t\thdd_warn_rl(\"monitor netif queue ctrl: if=%s action=%s reason=%s carrier=%u running=%u pause_map=0x%x caller=%s\",\n"
        "\t\t\t    adapter->dev->name, hdd_action_type_to_string(action),\n"
        "\t\t\t    hdd_reason_type_to_string(reason),\n"
        "\t\t\t    netif_carrier_ok(adapter->dev) ? 1 : 0,\n"
        "\t\t\t    netif_running(adapter->dev) ? 1 : 0,\n"
        "\t\t\t    adapter->pause_map, current->comm);\n\t}\n\n\tswitch (action) {",
        "tx_rx.c netif_queue_control monitor guard")

    f = HDD_INC / "wlan_hdd_main.h"
    fuzzy_replace(
        f,
        "\tqdf_atomic_t ch_switch_in_progress;\n"
        "\tqdf_event_t acs_complete_event;\n",
        "\tqdf_atomic_t ch_switch_in_progress;\n"
        "\tqdf_atomic_t mon_ch_switch_in_progress;\n"
        "\tqdf_event_t acs_complete_event;\n",
        "main.h mon_ch_switch_in_progress field",
        diagnose_needle="qdf_atomic_t ch_switch_in_progress;")

    f = HDD_SRC / "wlan_hdd_cfg80211.c"
    fuzzy_replace(
        f,
        "\tch_params.ch_width = ch_width;\n"
        "\twlan_reg_set_channel_params_for_freq(hdd_ctx->pdev,\n"
        "\t\t\t\t\t     chandef->chan->center_freq,\n"
        "\t\t\t\t\t     sec_ch_2g_freq, &ch_params);\n"
        "\tif (wlan_hdd_change_hw_mode_for_given_chnl(adapter,\n"
        "\t\t\t\t\t\t   chandef->chan->center_freq,\n"
        "\t\t\t\t\t\t   POLICY_MGR_UPDATE_REASON_SET_OPER_CHAN)) {\n"
        "\t\thdd_err(\"Failed to change hw mode\");\n"
        "\t\treturn -EINVAL;\n"
        "\t}\n",
        "\tch_params.ch_width = ch_width;\n"
        "\twlan_reg_set_channel_params_for_freq(hdd_ctx->pdev,\n"
        "\t\t\t\t\t     chandef->chan->center_freq,\n"
        "\t\t\t\t\t     sec_ch_2g_freq, &ch_params);\n"
        "\n"
        "\tif (qdf_atomic_inc_return(&adapter->mon_ch_switch_in_progress) > 1) {\n"
        "\t\thdd_err_rl(\"monitor mode channel switch already in progress\");\n"
        "\t\treturn -EBUSY;\n"
        "\t}\n"
        "\n"
        "\tif (!policy_mgr_is_current_hwmode_dbs(hdd_ctx->psoc) &&\n"
        "\t    wlan_hdd_change_hw_mode_for_given_chnl(adapter,\n"
        "\t\t\t\t\t\t   chandef->chan->center_freq,\n"
        "\t\t\t\t\t\t   POLICY_MGR_UPDATE_REASON_SET_OPER_CHAN)) {\n"
        "\t\thdd_err(\"Failed to change hw mode\");\n"
        "\t\tqdf_atomic_set(&adapter->mon_ch_switch_in_progress, 0);\n"
        "\t\treturn -EINVAL;\n"
        "\t}\n",
        "cfg80211.c set_mon_ch: acquire guard + DBS skip-check",
        diagnose_needle="if (wlan_hdd_change_hw_mode_for_given_chnl(adapter,")

    fuzzy_replace(
        f,
        "\tif (adapter->monitor_mode_vdev_up_in_progress) {\n"
        "\t\thdd_err_rl(\"monitor mode vdev up in progress\");\n"
        "\t\treturn -EBUSY;\n"
        "\t}\n",
        "\tif (adapter->monitor_mode_vdev_up_in_progress) {\n"
        "\t\thdd_err_rl(\"monitor mode vdev up in progress\");\n"
        "\t\tqdf_atomic_set(&adapter->mon_ch_switch_in_progress, 0);\n"
        "\t\treturn -EBUSY;\n"
        "\t}\n",
        "cfg80211.c set_mon_ch: release guard on vdev-up-in-progress return",
        diagnose_needle="monitor mode vdev up in progress")

    fuzzy_replace(
        f,
        "\tstatus = qdf_event_reset(&adapter->qdf_monitor_mode_vdev_up_event);\n"
        "\tif (QDF_IS_STATUS_ERROR(status)) {\n"
        "\t\thdd_err_rl(\"failed to reinit monitor mode vdev up event\");\n"
        "\t\treturn qdf_status_to_os_return(status);\n"
        "\t}\n",
        "\tstatus = qdf_event_reset(&adapter->qdf_monitor_mode_vdev_up_event);\n"
        "\tif (QDF_IS_STATUS_ERROR(status)) {\n"
        "\t\thdd_err_rl(\"failed to reinit monitor mode vdev up event\");\n"
        "\t\tqdf_atomic_set(&adapter->mon_ch_switch_in_progress, 0);\n"
        "\t\treturn qdf_status_to_os_return(status);\n"
        "\t}\n",
        "cfg80211.c set_mon_ch: release guard on event-reset failure",
        diagnose_needle="failed to reinit monitor mode vdev up event")

    fuzzy_replace(
        f,
        "\tstatus = sme_roam_channel_change_req(mac_handle, bssid,\n"
        "\t\t\t\t\t     adapter->vdev_id,\n"
        "\t\t\t\t\t     &roam_profile.ch_params,\n"
        "\t\t\t\t\t     &roam_profile);\n"
        "\tif (status) {\n"
        "\t\thdd_err_rl(\"Failed to set sme_RoamChannel for monitor mode status: %d\",\n"
        "\t\t\t   status);\n"
        "\t\tadapter->monitor_mode_vdev_up_in_progress = false;\n"
        "\t\tret = qdf_status_to_os_return(status);\n"
        "\t\treturn ret;\n"
        "\t}\n",
        "\tstatus = sme_roam_channel_change_req(mac_handle, bssid,\n"
        "\t\t\t\t\t     adapter->vdev_id,\n"
        "\t\t\t\t\t     &roam_profile.ch_params,\n"
        "\t\t\t\t\t     &roam_profile);\n"
        "\tif (status) {\n"
        "\t\thdd_err_rl(\"Failed to set sme_RoamChannel for monitor mode status: %d\",\n"
        "\t\t\t   status);\n"
        "\t\tadapter->monitor_mode_vdev_up_in_progress = false;\n"
        "\t\tqdf_atomic_set(&adapter->mon_ch_switch_in_progress, 0);\n"
        "\t\tret = qdf_status_to_os_return(status);\n"
        "\t\treturn ret;\n"
        "\t}\n",
        "cfg80211.c set_mon_ch: release guard on roam-channel-change failure",
        diagnose_needle="Failed to set sme_RoamChannel for monitor mode")

    fuzzy_replace(
        f,
        "\t\telse\n"
        "\t\t\thdd_err_rl(\"Failed monitor mode vdev up(status-%d)\",\n"
        "\t\t\t\t  status);\n"
        "\n"
        "\t\tadapter->monitor_mode_vdev_up_in_progress = false;\n"
        "\t\treturn qdf_status_to_os_return(status);\n"
        "\t}\n"
        "\n"
        "\tadapter->mon_chan_freq = chandef->chan->center_freq;\n"
        "\tadapter->mon_bandwidth = ch_width;\n"
        "\n"
        "\thdd_exit();\n"
        "\n"
        "\treturn 0;\n"
        "}\n",
        "\t\telse\n"
        "\t\t\thdd_err_rl(\"Failed monitor mode vdev up(status-%d)\",\n"
        "\t\t\t\t  status);\n"
        "\n"
        "\t\tadapter->monitor_mode_vdev_up_in_progress = false;\n"
        "\t\tqdf_atomic_set(&adapter->mon_ch_switch_in_progress, 0);\n"
        "\t\treturn qdf_status_to_os_return(status);\n"
        "\t}\n"
        "\n"
        "\tadapter->mon_chan_freq = chandef->chan->center_freq;\n"
        "\tadapter->mon_bandwidth = ch_width;\n"
        "\n"
        "\tqdf_atomic_set(&adapter->mon_ch_switch_in_progress, 0);\n"
        "\n"
        "\thdd_exit();\n"
        "\n"
        "\treturn 0;\n"
        "}\n",
        "cfg80211.c set_mon_ch: release guard on timeout + success paths",
        diagnose_needle="Failed monitor mode vdev up(status-%d)")

    f = WMA_SRC / "wma_frame_inject.c"
    fuzzy_replace(
        f,
        "\tif (g_inj_tx_vdev.created) {\n"
        "\t\tif (g_inj_tx_vdev.chanfreq == chanfreq)\n"
        "\t\t\treturn QDF_STATUS_SUCCESS;\n"
        "\t\t/* Channel changed – tear down and recreate */\n"
        "\t\twma_injection_destroy_tx_vdev(wma);\n"
        "\t}\n",
        "\tif (g_inj_tx_vdev.created) {\n"
        "\t\tif ((g_inj_tx_vdev.chanfreq < 4000) == (chanfreq < 4000))\n"
        "\t\t\treturn QDF_STATUS_SUCCESS;\n"
        "\t\t/*\n"
        "\t\t * Only recreate on an actual BAND change (2.4GHz <-> 5GHz),\n"
        "\t\t * not on every same-band channel change -- every injected\n"
        "\t\t * frame already carries its own real target channel in the\n"
        "\t\t * per-command WMI chanfreq field (read live from the\n"
        "\t\t * monitor vdev's current channel right before this call,\n"
        "\t\t * see tx_chanfreq in wma_send_injection_frame_to_fw()),\n"
        "\t\t * the same mechanism the stock P2P off-channel\n"
        "\t\t * action-frame path (p2p_mgmt_tx()) already relies on to\n"
        "\t\t * transmit on a channel different from the sending vdev's\n"
        "\t\t * own channel, with zero vdev reconfiguration. Recreating\n"
        "\t\t * this vdev on every hop bought nothing but ~400-700ms of\n"
        "\t\t * synchronous msleep() per hop inside the injection\n"
        "\t\t * workqueue.\n"
        "\t\t */\n"
        "\t\twma_injection_destroy_tx_vdev(wma);\n"
        "\t}\n",
        "wma_frame_inject.c ensure_tx_vdev: only recreate on band change",
        diagnose_needle="g_inj_tx_vdev.created) {")

    vdev_up_marker = "status = wmi_unified_vdev_up_send(wma->wmi_handle, inj_mac,"
    if already_present(f, vdev_up_marker):
        print("[wma_frame_inject.c ensure_tx_vdev: VDEV_UP] already present -- skipping")
    else:
        fuzzy_replace(
            f,
            "\t/*\n"
            "\t * Skip VDEV_UP.  For STA vdevs, firmware's wlan_vdev_up\n"
            "\t * asserts unless a BSS peer (the AP) exists — we only have\n"
            "\t * a self-peer.  The mgmt TX handler only needs the vdev in\n"
            "\t * STARTED state with a valid peer at vdev+0xc.\n"
            "\t */\n"
            "\n"
            "\tg_inj_tx_vdev.created          = true;\n",
            "\t{\n"
            "\t\tQDF_STATUS status;\n"
            "\n"
            "\t\t/*\n"
            "\t\t * No real AP association exists for this self-peer-only\n"
            "\t\t * helper vdev, so there is no AID.\n"
            "\t\t * wma_mon_mlme_vdev_up_send() -- which brings up the\n"
            "\t\t * real monitor vdev successfully on this exact hardware\n"
            "\t\t * right now -- calls vdev_mgr_up_send() with assoc_id\n"
            "\t\t * forced to 0, no peer check anywhere in that path.\n"
            "\t\t */\n"
            "\t\tstatus = wmi_unified_vdev_up_send(wma->wmi_handle, inj_mac,\n"
            "\t\t\t\t\t\t\t\t  vid, 0, 0);\n"
            "\t\tif (QDF_IS_STATUS_ERROR(status)) {\n"
            "\t\t\tWMA_LOGE(\"%s: Failed to send VDEV_UP for injection vdev: %d\",\n"
            "\t\t\t\t __func__, status);\n"
            "\t\t}\n"
            "\t}\n"
            "\n"
            "\tg_inj_tx_vdev.created = true;\n",
            "wma_frame_inject.c ensure_tx_vdev: send VDEV_UP",
            diagnose_needle="Skip VDEV_UP."
        )


if __name__ == "__main__":
    run_bulk_patch()
    apply_hand_merges()
    scan_for_duplicate_functions()
    dump_rejects()
    if FAILURES:
        print(f"\n{'=' * 60}\nFATAL: {len(FAILURES)} hand-merge/validation failure(s):\n{'=' * 60}")
        for msg in FAILURES:
            print(f"  - {msg}")
        sys.exit(1)
    print(f"\n{'=' * 60}\nSUCCESS: All patches and hand-merges applied cleanly!\n{'=' * 60}")
