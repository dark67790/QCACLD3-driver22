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
    # re.escape turns each space/tab into a literal char; collapse any run
    # of escaped horizontal whitespace into a flexible \s+ (but not \n).
    escaped = re.sub(r"(?:\\ |\\\t)+", r"[ \\t]+", escaped)
    return escaped


def _diagnose(path: Path, needle: str, context_lines: int = 70):
    """On an anchor miss, locate a much smaller near-certain substring and
    dump real surrounding file content to the CI log, so we get ground
    truth from a single run instead of a manual back-and-forth."""
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
        # render tabs visibly so indentation-only drift is obvious in the log
        print(f"    {i + 1:>6}\t{lines[i].replace(chr(9), '<TAB>')}")


def already_present(path: Path, needle: str) -> bool:
    """Idempotency check: some hunks succeed in the bulk patch step (possibly
    landing correctly even when the hand-merge step's wider anchor doesn't
    match due to surrounding tree-specific content). Check for the smallest
    reliable signal before attempting a hand-merge, so already-applied work
    isn't misreported as a failure."""
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
    """Dump full ground-truth context for every remaining .rej file in one
    pass: the literal expected hunk plus the real file's actual content at
    that location. Runs after Step 1 so any NEW divergence (files we have
    no hand-merge fix for yet) surfaces in this run's log instead of
    requiring another manual round-trip."""
    rejects = sorted(WLAN_DIR.rglob("*.rej"))
    if not rejects:
        print("\n(no .rej files after bulk patch step -- nothing to diagnose)")
        return
    print(f"\n{'=' * 60}\nDIAGNOSING {len(rejects)} REMAINING .rej FILE(S)\n{'=' * 60}")
    for rej in rejects:
        target = rej.with_suffix("")  # strip trailing .rej
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
    """Given just the '+' lines of ONE hunk (already de-prefixed, in
    order), find function definitions within that added block only.
    Scoped to a single hunk's additions -- never the full file -- so
    pre-existing #ifdef/#else branch-pairs elsewhere in the file never
    enter into this at all."""
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
    """Parse the patch itself (not any target tree) to find the narrow,
    specific list of function names it newly defines in EXISTING files
    (files without 'new file mode' -- brand new files can't collide with
    anything). Scoped to each hunk's own '+' lines only, which is what
    keeps this accurate: we're asking 'what does the patch add', not
    'find every function anywhere', so the vast pre-existing codebase
    (full of legitimate #ifdef/#else same-name branch-pairs) never enters
    into the analysis. Returns {relative_path: set(names)}."""
    patch_text = PATCH_FILE.read_text()
    parts = re.split(r"^diff --git a/(\S+) b/\S+$", patch_text, flags=re.M)
    pairs = list(zip(parts[1::2], parts[2::2]))
    result = {}
    for path, content in pairs:
        if not path.endswith(".c"):
            continue
        if "new file mode" in content[:500]:
            continue  # brand new file -- nothing pre-existing to collide with
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
    """For every line, compute the stack of (block_id, branch) identifying
    which #if/#ifdef nesting it's inside. Two lines are only genuinely
    mutually-exclusive if they share a common block_id but differ in
    branch at that id -- NOT just because some #else exists somewhere
    between them in the file (which could belong to a totally unrelated
    conditional for a different feature entirely, especially across a
    1000+ line gap)."""
    lines = text.split("\n")
    stack = []  # list of [block_id, branch]
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
        return False  # no common conditional at all -- not exclusive
    return any(a_map[bid] != b_map[bid] for bid in shared)


def _find_matching_paren(text: str, open_idx: int) -> int:
    """text[open_idx] must be '('. Returns the index of its matching ')',
    tracking nesting depth, or -1 if unbalanced before EOF."""
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
    """text[open_paren_idx] must be '('. A match is a genuine function
    DEFINITION only if its parameter list's matching ')' is followed
    (after only whitespace) by '{'. A call ends in ';' or ',' or ')'
    inside a larger expression; a forward declaration ends in ';'.
    Without this check, the duplicate-function scanner below false-flags
    an ordinary call site (e.g. `hdd_monitor_mode_tx_inject(adapter, dev,
    skb);`) as a second definition, since the scan regex only looks for
    "name(" at the start of a non-comment line and doesn't care what
    follows the closing paren."""
    close_idx = _find_matching_paren(text, open_paren_idx)
    if close_idx == -1:
        return False
    j = close_idx + 1
    n = len(text)
    while j < n and text[j] in " \t\r\n":
        j += 1
    return j < n and text[j] == "{"


def _delete_function_by_doc_marker(path: Path, doc_marker: str, label: str) -> bool:
    """Delete one complete function definition, located structurally
    instead of by a literal transcription of its full body: find the
    short, distinctive doc_marker inside its kernel-doc comment, walk
    back to that comment's opening '/**', walk forward to the function's
    opening '{', then brace-balance to the matching closing '}' and
    delete everything from the comment through that brace (plus one
    trailing blank line, if present).

    This replaces a fragile literal-body fuzzy_replace for cases like
    wlan_hdd_cfg80211_get_channel, where the body itself (not just its
    surroundings) has already been shown to drift between snapshots --
    a hand-transcribed anchor for the whole function keeps re-breaking
    on each new divergence inside the body. Brace-matching is correct
    regardless of what's inside, as long as the doc comment and a '{'
    immediately following the signature are present -- true for every
    kernel-doc-commented function in this codebase."""
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

    # Consume up to one trailing blank line after the closing brace so we
    # don't leave a double blank line behind.
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
    """Dump real context around each occurrence of a duplicated function name,
    the same way _diagnose() does for a missed .rej anchor, so a genuine new
    divergence (one this script has no hand-merge logic for yet) gets ground
    truth in this run's log instead of requiring a guessed fix."""
    lines = path.read_text().splitlines()
    for ln in lines_list:
        lo = max(0, ln - 1 - context_lines // 2)
        hi = min(len(lines), ln - 1 + context_lines // 2)
        print(f"    DIAGNOSE DUPLICATE '{name}' at line {ln} in {path.name} "
              f"(showing {lo + 1}-{hi}):")
        for i in range(lo, hi):
            print(f"    {i + 1:>6}\t{lines[i].replace(chr(9), '<TAB>')}")


def scan_for_duplicate_functions():
    """For the narrow, patch-derived list of function names (see
    get_patch_added_function_names), check whether the FINAL merged file
    defines each name more than once -- and if so, whether the two
    definitions sit in mutually-exclusive #ifdef/#else branches (legal,
    normal kernel style, not a bug) versus truly both being live (the
    bug class behind hdd_disable_monitor_mode and
    wlan_hdd_cfg80211_get_channel). Runs after hand-merges so it reflects
    final state."""
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
            # (?!\s*[*/]) excludes comment lines -- kernel doc convention
            # writes "* funcname() - description" which would otherwise
            # false-match. [\w \t\*]  (not \s) keeps the flexible middle
            # portion from crossing newlines, since \s matches '\n' too
            # and would otherwise let this bridge into unrelated lines.
            pattern = re.compile(
                r"^(?!\s*[*/])(?:static[ \t]+)?(?:inline[ \t]+)?"
                r"[\w \t\*]*?\b" + re.escape(name) + r"[ \t]*\(", re.M)
            # Pattern alone can't distinguish a definition from a call or a
            # forward declaration -- "name(" at line-start matches a call
            # like "hdd_monitor_mode_tx_inject(adapter, dev, skb);" just as
            # readily as the real definition. Confirm each match's parameter
            # list is followed by '{' (a definition) before counting it.
            match_lines = []
            for m in pattern.finditer(text):
                open_paren_idx = m.end() - 1  # pattern consumes the '(' itself
                if (text[open_paren_idx] == "("
                        and _is_function_definition(text, open_paren_idx)):
                    match_lines.append(text.count("\n", 0, m.start()) + 1)
            if len(match_lines) <= 1:
                print(f"  ok  {name}  ({rel}) -- {len(match_lines)} occurrence(s)")
                continue
            # 2+ occurrences -- legitimate only if every consecutive pair
            # is genuinely under mutually-exclusive branches of the SAME
            # conditional (shared block_id, different branch), not just
            # separated by an unrelated #else somewhere else in the file.
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

    # --- rx_monitor.h : hdd_disable_monitor_mode(void) already exists
    #     natively in this tree (confirmed: declared at the prototype AND
    #     stub, called from main.c with no args) -- it predates this patch
    #     and is unrelated to it. The patch's own (struct net_device *dev)
    #     version is dead code (grep of the full 44-file patch confirms
    #     nothing calls it), so we do NOT add a conflicting declaration
    #     here. See the wlan_hdd_rx_monitor.c fix below instead, which
    #     removes the patch's duplicate definition and splices the one
    #     line that matters (hdd_frame_inject_disable) into the real fn. ---
    f = HDD_SRC / "wlan_hdd_rx_monitor.h"
    if already_present(f, "hdd_disable_monitor_mode("):
        print("[rx_monitor.h] hdd_disable_monitor_mode already present "
              "(this tree's own, unrelated function) -- correctly left alone")
    else:
        msg = ("[rx_monitor.h] expected hdd_disable_monitor_mode to already "
               "exist in this tree and it doesn't -- tree has changed more "
               "than expected, needs a human to re-check")
        FAILURES.append(msg)

    # --- rx_monitor.c : conditional include (often already landed via the
    #     bulk patch step even when the hand-merge anchor below doesn't
    #     match due to tree-specific surrounding content) ---
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

    # --- rx_monitor.c : the patch adds its OWN hdd_disable_monitor_mode()
    #     with a (struct net_device *dev) signature, which collides with
    #     this tree's pre-existing (void)-signature function of the same
    #     name -- confirmed via live grep as two full definitions in the
    #     same file, which will not compile. Nothing in the 44-file patch
    #     ever calls the (dev) version, so it's safe to delete outright;
    #     splice its one useful line (hdd_frame_inject_disable) into the
    #     real (void) function instead, using hdd_get_adapter(hdd_ctx,
    #     QDF_MONITOR_MODE) to reach the monitor adapter without a dev
    #     pointer in scope -- the same idiom this tree already uses
    #     elsewhere (wlan_hdd_state_ctrl_param_write) to find it. ---
    # The patch's own duplicate (struct net_device *dev) version may or may
    # not have landed depending on fuzz tolerance in Step 1 -- handle both
    # outcomes instead of assuming one. Either way the end state should be:
    # exactly one hdd_disable_monitor_mode(void), with frame-inject-disable
    # spliced in via hdd_get_adapter(hdd_ctx, QDF_MONITOR_MODE).
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
        # fuzzy match let the patch's conflicting duplicate land -- remove
        # it entirely (confirmed unused anywhere in the 44-file patch) and
        # splice into the real function in one combined edit.
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
        # Common case under tighter fuzz: the duplicate never landed, the
        # tree's original single function is untouched -- extend it directly.
        fuzzy_replace(f, void_fn, spliced_void_fn,
                      "rx_monitor.c splice into existing fn (no duplicate present)",
                      diagnose_needle="int hdd_disable_monitor_mode(void)")

    # --- cfg80211.c : conditional include + vendor cmd array entry ---
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

    # --- main.h : injection_ctx field (often already landed via the bulk
    #     patch step even when the hand-merge anchor below doesn't match
    #     due to tree-specific fields between the two anchor lines) ---
    f = HDD_INC / "wlan_hdd_main.h"
    if already_present(f, "struct hdd_injection_ctx *injection_ctx;"):
        print("[main.h injection_ctx field] already present -- skipping")
    else:
        # Confirmed stable across two independent runs: this single line is
        # a reliable anchor regardless of what surrounds it (field order /
        # #ifdef blocks around it drift between runs/snapshots, this doesn't).
        fuzzy_replace(f,
            "\tqdf_atomic_t net_dev_hold_ref_count[NET_DEV_HOLD_ID_MAX];\n",
            "\tqdf_atomic_t net_dev_hold_ref_count[NET_DEV_HOLD_ID_MAX];\n"
            "#ifdef FEATURE_FRAME_INJECTION_SUPPORT\n"
            "\tstruct hdd_injection_ctx *injection_ctx;\n#endif\n",
            "main.h injection_ctx field",
            diagnose_needle="net_dev_hold_ref_count[NET_DEV_HOLD_ID_MAX]")

    # --- main.c : __hdd_mon_open -- set carrier/queues up, keep existing
    #     BBM policy block this tree already has that the patch's baseline
    #     didn't ---
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

    # --- wma.h : legacy WMA_LOGE/W/D/I macro compatibility shim. The
    #     patch was authored against an older baseline where this family
    #     was still the logging convention in core/wma/src/*.c -- confirmed
    #     by context lines in the patch itself (e.g. the unchanged
    #     "WMA_LOGE(...)" line around the Tx-complete-callback check) that
    #     no longer match this tree's actual content there at all (this
    #     tree already reads wma_err()/wma_warn()/etc. at those spots,
    #     confirmed via live grep -- the module-wide rename predates this
    #     patch and is unrelated to it). WMA_LOGE itself is not defined
    #     ANYWHERE in this tree (confirmed via a repo-wide grep turning up
    #     zero #define sites), so every "+" line the patch adds using it
    #     is an implicit-function-declaration compile error, confirmed via
    #     live CI across all three files that carry it: the new
    #     wma_frame_inject.c (7 call sites, confirmed to #include "wma.h"
    #     as its first header), plus one hand-merged call each in
    #     wma_main.c (this fix, below) and wma_mgmt.c (landed via the bulk
    #     patch step, no hand-merge needed). Fixing each call site
    #     individually can't be fully verified in one CI round-trip since
    #     parallel make aborts after the first failures, so a build might
    #     not even reach every file that has the problem yet -- one
    #     compatibility shim in wma.h, right next to the real wma_err/
    #     wma_warn/wma_debug/wma_info definitions it maps onto, reaches
    #     every file that already #include "wma.h" (which every WMA-layer
    #     source file in this patch does) regardless of how far the build
    #     gets. WMA_LOGW/D/I are included too even though only WMA_LOGE is
    #     confirmed to land in compiled code -- the mapping itself is
    #     fully grounded (wma.h's own real macros, same params... call
    #     convention), so including the other three is a zero-risk hedge,
    #     not a guess. ---
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

    # --- wma_main.c : wma_open -- inject queue init, after this tree's
    #     extra wma_trace_init() call ---
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

    # --- wma_utils.c : wma_mon_mlme_vdev_down_send -- del_bss_resp alloc.
    #     The patch's hunk for this function is actually TWO separate
    #     hunks: one adding "struct del_bss_resp *resp;" at the top of the
    #     function (@@ -4690,6 +4690,7 @@, a 1-line addition), and one
    #     adding the resp=alloc/use block near the end (@@ -4703,10
    #     +4704,15 @@). Only the second one had hand-merge logic in this
    #     script -- the first was silently dropped as an unaddressed .rej
    #     (the file was already in ATTEMPTED_FILES because of the second
    #     hunk's fix, so check_unaddressed_rejects() never flagged it).
    #     Confirmed via live CI: 'use of undeclared identifier resp' at
    #     every use site below. struct del_bss_resp is a real, unrenamed
    #     type (wlan_mlme_main.h, fields status/vdev_id -- matches what the
    #     usage code assumes) and both wma_internal.h and wlan_mlme_main.h
    #     are already included in this file, so only the local declaration
    #     itself is missing.
    #     Anchored on the function's own signature (unique -- the name
    #     appears once) rather than the declaration list's text alone,
    #     since wma_mon_mlme_vdev_stop_send() right above it has an
    #     identical-looking 3-line declaration block that would otherwise
    #     make the anchor ambiguous. Inserted as the LAST declaration,
    #     before the function's first statement -- not right before its
    #     point of use -- to avoid the exact -Wdeclaration-after-statement
    #     class of bug already hit and fixed once this run in
    #     __hdd_hard_start_xmit. ---
    #     UPDATE (confirmed via a second live CI round): the bulk patch
    #     step's --fuzz=2 actually DID apply this declaration hunk on a
    #     later run -- just into the wrong function. wma_wlm_stats_req(),
    #     an unrelated function with its own "QDF_STATUS status;" as its
    #     second-to-last declaration, matched the hunk's thin surviving
    #     context well enough under heavy fuzz, so "struct del_bss_resp
    #     *resp;" landed there instead -- as a dead, never-referenced
    #     local, confirmed via live CI dump. The v8 idempotency check
    #     (already_present(f, "struct del_bss_resp *resp;"), a bare
    #     file-wide substring search with no location awareness) saw that
    #     text existed SOMEWHERE in the file and concluded the fix was
    #     already done, silently skipping the real insertion into
    #     wma_mon_mlme_vdev_down_send entirely -- exactly the kind of gap
    #     check_unaddressed_rejects() was just hardened against, except
    #     this one lives in an idempotency check instead. Fixed by scoping
    #     both checks to the actual function bodies: clean up the
    #     misplaced declaration in wma_wlm_stats_req (also sidesteps a
    #     likely -Wunused-variable/-Werror once it's unused there), and
    #     check for the REAL fix's presence using the full anchor-plus-
    #     declaration text, not the declaration text alone. ---
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
        print("[wma_utils.c del_bss_resp declaration] already present in "
              "wma_mon_mlme_vdev_down_send -- skipping")
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

    # --- Kbuild : wire the new object files + cppflag into the build.
    #     Three separate insertion points, confirmed against real file
    #     content (patch's own hunks reported "Reversed or previously
    #     applied" against this tree, so its context can't be trusted --
    #     these three anchors were pulled directly from the real file). ---
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

    # --- cfg80211.c : the patch adds its own simplified
    #     wlan_hdd_cfg80211_get_channel(), which collides with this tree's
    #     own, more complete SSR-safe wrapper (__wlan_hdd_cfg80211_get_channel
    #     + osif_vdev_sync_op_start/stop pattern) of the same name. Confirmed
    #     via a live compile error (redefinition). Identify the patch's
    #     version by its distinctive docstring text (unique to this patch,
    #     not generic CAF prose) rather than line numbers, since those drift.
    #     .get_channel isn't on frame-injection's critical path (pure
    #     userspace channel introspection), so deleting it outright is safe
    #     -- no need to port its mon_chan_freq fallback into the real fn.
    #
    #     NOTE: after three rounds of a hand-transcribed full-body anchor
    #     re-breaking on each newly-discovered internal divergence (first
    #     the missing "Primary source"/"Fallback" comments, then another
    #     mismatch further into the body that a 70-line diagnose window
    #     couldn't even show), the literal-body approach was abandoned.
    #     _delete_function_by_doc_marker() below finds this function
    #     structurally -- by its doc comment, then brace-matching to the
    #     closing '}' -- so it's correct no matter what the body contains. ---
    f = HDD_SRC / "wlan_hdd_cfg80211.c"
    patch_get_channel_marker = "so that tools like aireplay-ng / mdk3 can determine the current channel."
    if not already_present(f, patch_get_channel_marker):
        print("[cfg80211.c get_channel dedup] patch's duplicate not present -- skipping")
    else:
        _delete_function_by_doc_marker(
            f, patch_get_channel_marker,
            "cfg80211.c remove duplicate get_channel")

        # Drop one of the two identical .get_channel = ... designated
        # initializers in wlan_hdd_cfg80211_ops (disambiguate via the
        # unique neighboring .set_monitor_channel line; keep the
        # unconditional one near .get_antenna so get_channel always works,
        # not only when FEATURE_MONITOR_MODE_SUPPORT is on).
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

    # --- default_defconfig : enable the feature flag ---
    f = CONFIGS / "default_defconfig"
    fuzzy_replace(f,
        "CONFIG_FEATURE_MONITOR_MODE_SUPPORT := y\n",
        "CONFIG_FEATURE_MONITOR_MODE_SUPPORT := y\n"
        "CONFIG_FEATURE_FRAME_INJECTION_SUPPORT := y\n",
        "default_defconfig injection flag")

    # --- qca_vendor.h : the patch's antenna-diversity-probe hunk landed in
    #     entirely the wrong enum. Its context was only "RX_NSS = 78," +
    #     blank line before the insertion, and the generic "/* keep last */"
    #     / AFTER_LAST / MAX idiom after it -- and that idiom repeats
    #     verbatim across dozens of unrelated enums in this header. Under
    #     --fuzz=2, patch dropped the (now-distant, since this tree's
    #     enum qca_wlan_vendor_attr_config has grown well past RX_NSS=78
    #     since the patch's baseline) trailing-context requirement and
    #     spliced the six new attrs into the next "keep last" block it
    #     found instead: enum qca_wlan_vendor_attr_acs_offload, right
    #     after QCA_WLAN_VENDOR_ATTR_ACS_EHT_ENABLED = 19. Confirmed via
    #     direct file dump in CI. This silently bloats acs_offload's own
    #     MAX from 19 to 129 (harmless by itself today, nothing reads
    #     those indices there -- but wrong, and worth not leaving in
    #     place) while leaving qca_wlan_vendor_attr_config's own MAX at
    #     81, which is what wlan_hdd_cfg80211.c's designated initializers
    #     for these same six attrs then fail to fit inside.
    #     Fix: cut the six-entry block out of acs_offload's tail and
    #     splice it into config's own tail instead, right after its real
    #     last member (QCA_WLAN_VENDOR_ATTR_CONFIG_ARP_NS_OFFLOAD = 81)
    #     and before ITS "/* keep last */". Each entry keeps its explicit
    #     value (124-129), so only the two AFTER_LAST/MAX pairs move:
    #     config's 82/81 -> 130/129 (matching every nla_parse() site that
    #     already reads _CONFIG_MAX), acs_offload's back down to 20/19. ---
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
        print("[qca_vendor.h ant_div relocation] already in the correct "
              "enum (qca_wlan_vendor_attr_config) -- skipping")
    elif not already_present(f, short_misplaced_marker):
        msg = ("[qca_vendor.h ant_div relocation] six-entry block not found "
               "at its known-wrong location (acs_offload's tail) and not "
               "found at the correct location either -- tree has drifted "
               "further than expected, needs a human to re-check")
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

    # --- tx_rx.c : radiotap include. The patch's own include-block hunk
    #     (@@ -59,6 +59,9 @@, confirmed directly from the patch file) adds
    #     the FEATURE_FRAME_INJECTION_SUPPORT guard for
    #     wlan_hdd_frame_inject.h, but never adds an unconditional
    #     #include <net/ieee80211_radiotap.h> anywhere. This tree already
    #     has a conditional radiotap include further down:
    #         #if defined(WLAN_SUPPORT_RX_FISA)
    #         #include "dp_fisa_rx.h"
    #         #else
    #         #include <net/ieee80211_radiotap.h>
    #         #endif
    #     -- so in any build where WLAN_SUPPORT_RX_FISA is defined, that
    #     #else branch is skipped and the injection code's use of struct
    #     ieee80211_radiotap_header / ieee80211_get_radiotap_len() hits an
    #     incomplete-type error (confirmed via live CI compile log). Add our
    #     own unconditional include, guarded only by
    #     FEATURE_FRAME_INJECTION_SUPPORT, right next to the include the
    #     patch already added -- header guards make the (harmless) double
    #     include under !WLAN_SUPPORT_RX_FISA builds a non-issue. ---
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

    # --- tx_rx.c : __hdd_hard_start_xmit -- xmit probe log + injection
    #     check landed in the wrong function. The patch's own hunk header
    #     (@@ -959,6 +1110,27 @@ static void __hdd_hard_start_xmit(struct
    #     sk_buff *skb), confirmed directly from the patch file) proves this
    #     block was authored against __hdd_hard_start_xmit, where dev, skb,
    #     and adapter (= WLAN_HDD_GET_PRIV_PTR(dev)) are all in scope. Under
    #     --fuzz=2 it spliced instead into hdd_register_rx_ol_cb(struct
    #     hdd_context *hdd_ctx, bool wifi3_0_target) -- both functions
    #     happen to have "void *soc = cds_get_context(QDF_MODULE_ID_SOC);"
    #     as a declaration line followed by a blank line, and that's all
    #     the surviving context needed to match under heavy fuzz once the
    #     real distance to the patch's original anchor grew too large.
    #     Confirmed via live CI: 'use of undeclared identifier dev/adapter/
    #     skb' at exactly the lines this block occupies inside
    #     hdd_register_rx_ol_cb, which only takes (hdd_ctx, wifi3_0_target)
    #     and never has dev/adapter/skb in scope at all.
    #     Fix: cut the block out of hdd_register_rx_ol_cb and splice it
    #     into __hdd_hard_start_xmit -- anchored on the END of the
    #     function's declaration list (now "int cpu =
    #     qdf_get_smp_processor_id();", the last declaration before
    #     "#ifdef QCA_WIFI_FTM" in the current tree), not on an arbitrary
    #     declaration line in the middle of the list. The patch's own hunk
    #     context confirms this -- its post-context shows "#ifdef
    #     QCA_WIFI_FTM" as the line immediately following the inserted
    #     block in its original baseline, where "void *soc = ...;" WAS the
    #     last declaration. This tree has since grown that declaration list
    #     (subtype, is_eapol, is_dhcp, stats, cpu all came later), but the
    #     adjacency to "#ifdef QCA_WIFI_FTM" is still the real anchor.
    #     (An earlier version of this fix anchored on "void *soc = ...;"
    #     instead and inserted a statement block ahead of later
    #     declarations -- compiles clean with GCC by default but fails
    #     under this kernel's -Wdeclaration-after-statement, confirmed via
    #     live CI: "ISO C90 forbids mixing declarations and code" pointing
    #     at the now-downstream "enum qdf_proto_subtype subtype = ...;".
    #     Inserting after the LAST declaration avoids this categorically,
    #     matching the patch's actual original intent.) ---
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
    # Anchored on the END of __hdd_hard_start_xmit's declaration list (the
    # last declaration, "int cpu = ...;", immediately followed by "#ifdef
    # QCA_WIFI_FTM") -- see comment above for why, after the
    # -Wdeclaration-after-statement failure on an earlier mid-declaration
    # anchor.
    xmit_probe_decl_end = "\tint cpu = qdf_get_smp_processor_id();\n"
    xmit_probe_ftm_guard = "\n#ifdef QCA_WIFI_FTM\n"
    xmit_probe_fixed_marker = (
        xmit_probe_decl_end + xmit_probe_block + xmit_probe_ftm_guard
    )

    if already_present(f, xmit_probe_fixed_marker):
        print("[tx_rx.c xmit_probe relocation] already in "
              "__hdd_hard_start_xmit -- skipping")
    elif not already_present(f, xmit_probe_misplaced_marker):
        msg = ("[tx_rx.c xmit_probe relocation] block not found at its "
               "known-wrong location (hdd_register_rx_ol_cb) and not found "
               "at the correct location either -- tree has drifted further "
               "than expected, needs a human to re-check")
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
                "tx_rx.c insert xmit_probe block into __hdd_hard_start_xmit "
                "(after last declaration, before first statement)",
                diagnose_needle="int cpu = qdf_get_smp_processor_id();")

    # --- tx_rx.c : wlan_hdd_netif_queue_control -- monitor-interface
    #     carrier/queue debug guard, after this tree's extra MLO
    #     hdd_adapter_is_link_adapter() guard ---
    f = HDD_SRC / "wlan_hdd_tx_rx.c"
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


def check_unaddressed_rejects():
    """Flag any .rej that looks unaddressed in the final tree.

    Originally this only checked whether the target FILE had any
    hand-merge logic at all (ATTEMPTED_FILES) -- but that's too coarse: a
    file can have working hand-merge logic for one hunk while a SEPARATE
    hunk against the same file is silently dropped, because the file being
    in ATTEMPTED_FILES (for the first hunk) hid the second hunk's reject
    from ever being flagged. This happened for real: wma_utils.c had
    correct hand-merge logic for its resp=alloc/use hunk, but a separate,
    earlier hunk adding "struct del_bss_resp *resp;" had none -- its .rej
    was swept away by cleanup() unflagged, and the gap only surfaced as a
    live CI compile failure ('use of undeclared identifier resp').

    So beyond the file-level check, for every remaining .rej whose target
    file DOES have other hand-merge logic, check whether this specific
    reject's own added ("+") lines actually made it into the final file
    somewhere (regardless of exact position -- hand-merged text may
    legitimately differ in whitespace/surroundings from the literal
    reject). If most of a reject's added content is nowhere in the file,
    it's very likely a hunk nothing has ever actually handled."""
    for rej in sorted(WLAN_DIR.rglob("*.rej")):
        rel = str(rej.relative_to(WLAN_DIR))
        if rel in KNOWN_INAPPLICABLE_REJECTS:
            print(f"[known-inapplicable] {rel} -- {KNOWN_INAPPLICABLE_REJECTS[rel]}")
            continue
        target = rej.with_suffix("").resolve()
        if target not in ATTEMPTED_FILES:
            FAILURES.append(
                f"[unaddressed] {rej.relative_to(WLAN_DIR)} -- no hand-merge "
                f"logic exists for this file yet, see dump_rejects() output "
                f"above for ground truth"
            )
            continue
        if not target.exists():
            continue
        rej_text = rej.read_text()
        added_lines = [
            l[1:].strip() for l in rej_text.split("\n")
            if l.startswith("+") and not l.startswith("+++") and l[1:].strip()
        ]
        if not added_lines:
            continue
        target_text = target.read_text()
        missing = [l for l in added_lines if l not in target_text]
        # Slack for legitimately-reworded hand merges: only flag when a
        # clear majority of THIS reject's added lines are nowhere in the
        # file at all, not just absent at the reject's original position.
        if len(missing) > len(added_lines) * 0.5:
            FAILURES.append(
                f"[unaddressed] {rej.relative_to(WLAN_DIR)} -- {target.name} "
                f"has hand-merge logic for other hunks, but {len(missing)}/"
                f"{len(added_lines)} of THIS reject's added lines are not "
                f"present anywhere in the final file -- likely a hunk this "
                f"script doesn't actually handle yet, see dump_rejects() "
                f"output above for ground truth"
            )


def cleanup():
    removed = 0
    for pattern in ("*.rej", "*.orig"):
        for p in QCACLD.rglob(pattern):
            p.unlink()
            removed += 1
    if removed:
        print(f"\nRemoved {removed} leftover .rej/.orig file(s) "
              f"(see FAILED list below if any were never addressed)")


def write_error_report():
    """Write this run's outcome to a fixed, predictable path so CI can
    archive it as a build artifact (via upload-artifact + if: always())
    regardless of exit code. The terminal log is easy to lose or truncate
    on a long build; this file isn't, and it's a stable target to wire an
    upload step to without having to touch this script again later."""
    report_path = REPO_ROOT / "injection_patch_report.log"
    lines = []
    if FAILURES:
        lines.append(f"FAILED: {len(FAILURES)} hand-merge fix(es) did not apply:")
        lines.extend(f"  - {msg}" for msg in FAILURES)
    else:
        lines.append("All hunks applied or hand-merged successfully.")
    report_path.write_text("\n".join(lines) + "\n")
    print(f"\n[error report] wrote {report_path.relative_to(REPO_ROOT)}")


def main():
    run_bulk_patch()
    dump_rejects()
    apply_hand_merges()
    check_unaddressed_rejects()
    scan_for_duplicate_functions()
    cleanup()
    write_error_report()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} hand-merge fix(es) did not apply:")
        for msg in FAILURES:
            print(f"  - {msg}")
        print(
            "\nThis means the tree has drifted further since these fixes "
            "were derived by hand against a specific snapshot. Re-check the "
            "actual file content at the point of failure and update the "
            "corresponding old_block/new_block in this script."
        )
        sys.exit(1)

    print("All hunks applied or hand-merged successfully.")
    print(
        "Deliberately skipped (non-essential / real risk of breaking "
        "tree-specific logic):\n"
        "  - main.c wlan_off/on root-UID gating while monitor active\n"
        "  - main.c HDD_FW_VER_SUB_ID macro fix (unrelated)\n"
        "  - Kbuild -Wmaybe-uninitialized / -Wframe-larger-than block (unrelated)"
    )


if __name__ == "__main__":
    main()
