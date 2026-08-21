"""UIAutomator-based interactive functions for app consent clicking, swiping, camera shooting.

Used by memstress and ad-hoc trace scripts. Requires uiautomator on the device.
"""
from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree

from .adb_utils import adb_shell_cp

# ---------------------------------------------------------------------------
# Default text match / exclude lists for consent dialogs
# ---------------------------------------------------------------------------
CONSENT_MATCH_TEXTS = [
    "同意",
    "继续",
    "允许",
    "确定",
    "接受",
    "Accept",
    "Agree",
    "OK",
    "Allow",
    "Continue",
]

CONSENT_EXCLUDE_TEXTS = [
    "不同意",
    "不允许",
    "不接受",
    "不继续",
    "Disagree",
    "Decline",
]

# ---------------------------------------------------------------------------
# Interaction dispatch map: package -> interaction function name
# ---------------------------------------------------------------------------
INTERACTION_MAP: Dict[str, str] = {
    "com.ss.android.ugc.aweme": "douyin",
    "com.ss.android.ugc.aweme.lite": "douyin",
    "com.smile.gifmaker": "douyin",
    "com.kuaishou.nebula": "douyin",
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_parent_map(root):
    """Build element -> parent dict for ancestor traversal."""
    parent_map = {}
    stack = [(root, None)]
    while stack:
        elem, parent = stack.pop()
        if parent is not None:
            parent_map[elem] = parent
        for child in elem:
            stack.append((child, elem))
    return parent_map


def _extract_bounds(bounds_str: str) -> Optional[Tuple[int, int, int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return None


def _find_clickable_ancestor(elem, parent_map) -> Optional:
    cur = elem
    for _ in range(20):
        if (cur.attrib.get("clickable") or "").lower() == "true":
            return cur
        cur = parent_map.get(cur)
        if cur is None:
            break
    return None


def _tap(serial: str, x: int, y: int) -> bool:
    cp = adb_shell_cp(serial, f"input tap {x} {y}", timeout_s=5, check=False)
    return cp.returncode == 0


def _swipe(serial: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> bool:
    cp = adb_shell_cp(serial, f"input swipe {x1} {y1} {x2} {y2} {duration_ms}", timeout_s=5, check=False)
    return cp.returncode == 0

# ---------------------------------------------------------------------------
# Core: dump UI and click matching text
# ---------------------------------------------------------------------------


def dump_ui(serial: str, dump_file: str = "/data/local/tmp/interactive_ui.xml") -> Optional:
    """Dump current screen UI hierarchy via uiautomator, return parsed XML root or None."""
    cp = adb_shell_cp(serial, f"uiautomator dump {dump_file}", timeout_s=15, check=False)
    if cp.returncode != 0:
        return None
    time.sleep(0.3)
    cp = adb_shell_cp(serial, f"cat {dump_file}", timeout_s=12, check=False)
    data = (cp.stdout or "").strip()
    if not data:
        return None
    idx = data.find("<?xml")
    if idx < 0:
        return None
    try:
        return ElementTree.fromstring(data[idx:])
    except Exception:
        return None


def find_element_by_resource_id(
    root,
    resource_id_substr: str,
) -> List[Tuple[int, int, str]]:
    """Scan parsed UI tree and return [(cx, cy, resource_id)] for elements whose
    resource-id contains the given substring and is clickable.

    Useful for finding specific widgets like shutter_button, play_button, etc.
    """
    results: List[Tuple[int, int, str]] = []
    for elem in root.iter():
        rid = (elem.attrib.get("resource-id") or "").strip()
        if resource_id_substr not in rid:
            continue
        clickable = (elem.attrib.get("clickable") or "").lower()
        if clickable != "true":
            continue
        bounds = _extract_bounds(elem.attrib.get("bounds", ""))
        if bounds is None:
            continue
        x1, y1, x2, y2 = bounds
        if x2 <= x1 or y2 <= y1:
            continue
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        results.append((cx, cy, rid))
    return results


def find_clickable_by_text(
    root,
    parent_map,
    match_texts: Sequence[str],
    exclude_texts: Sequence[str] = (),
) -> List[Tuple[int, int, str]]:
    """Scan parsed UI tree and return [(cx, cy, snippet)] for elements whose
    text or content-desc contains any match_texts substring but no exclude_texts.
    """
    results: List[Tuple[int, int, str]] = []
    for elem in root.iter():
        text = (elem.attrib.get("text") or "").strip()
        cd = (elem.attrib.get("content-desc") or "").strip()
        combined = f"{text} {cd}".strip()
        if not combined:
            continue
        if exclude_texts and any(ex in combined for ex in exclude_texts):
            continue
        matched = next((kw for kw in match_texts if kw in combined), None)
        if not matched:
            continue

        clickable = _find_clickable_ancestor(elem, parent_map)
        if clickable is None:
            clickable = elem
        bounds = _extract_bounds(clickable.attrib.get("bounds", ""))
        if bounds is None:
            continue
        x1, y1, x2, y2 = bounds
        if x2 <= x1 or y2 <= y1:
            continue
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        results.append((cx, cy, combined[:120]))
    return results


def click_text_on_screen(
    serial: str,
    match_texts: Optional[Sequence[str]] = None,
    exclude_texts: Optional[Sequence[str]] = None,
    dump_file: str = "/data/local/tmp/interactive_ui.xml",
) -> List[str]:
    """Dump current UI, find elements whose text/content-desc matches any keyword
    from match_texts (but not exclude_texts), and tap their clickable ancestor.

    Returns list of matched text snippets that were clicked.
    """
    if match_texts is None:
        match_texts = CONSENT_MATCH_TEXTS
    if exclude_texts is None:
        exclude_texts = CONSENT_EXCLUDE_TEXTS

    root = dump_ui(serial, dump_file)
    if root is None:
        return []

    parent_map = _build_parent_map(root)
    targets = find_clickable_by_text(root, parent_map, match_texts, exclude_texts)

    clicked: List[str] = []
    for cx, cy, snippet in targets:
        if _tap(serial, cx, cy):
            clicked.append(snippet)
    return clicked


def interactive_click_loop(
    serial: str,
    match_texts: Optional[Sequence[str]] = None,
    exclude_texts: Optional[Sequence[str]] = None,
    rounds: int = 4,
    gap_s: float = 0.8,
) -> List[str]:
    """Run multiple rounds of UI scan-and-click to handle multi-step dialogs.
    Stops early when no matches are found.
    """
    all_clicked: List[str] = []
    for _ in range(rounds):
        time.sleep(gap_s)
        clicked = click_text_on_screen(serial, match_texts, exclude_texts)
        all_clicked.extend(clicked)
        if not clicked:
            break
    return all_clicked

# ---------------------------------------------------------------------------
# App-specific interactions
# ---------------------------------------------------------------------------


def interact_douyin(serial: str, swipes: int = 5, gap_s: float = 2.0) -> Dict:
    """Launch 抖音 and swipe up through the video feed to trigger video playback.

    Each swipe scrolls to the next video, which triggers MediaCodec decode
    and associated DMA-BUF allocations.

    Returns dict with keys: swipes, swiped, errors.
    """
    result: Dict = {"swipes": swipes, "swiped": 0, "errors": 0}

    adb_shell_cp(serial, "am force-stop com.ss.android.ugc.aweme", timeout_s=8)
    time.sleep(0.5)

    cp = adb_shell_cp(
        serial,
        "am start -n com.ss.android.ugc.aweme/.splash.SplashActivity",
        timeout_s=15,
        check=False,
    )
    ok = cp.returncode == 0 and "Error:" not in (cp.stdout or "")
    if not ok:
        result["errors"] = 1
        result["launch_error"] = (cp.stdout or "")[:200]
        return result

    # Wait for splash + first video to load
    time.sleep(4.0)

    # Screen is 1080x2400. Swipe from lower-mid to upper-mid.
    x_center = 540
    y_from = 1800
    y_to = 600
    duration = 300

    for i in range(swipes):
        ok = _swipe(serial, x_center, y_from, x_center, y_to, duration)
        if ok:
            result["swiped"] += 1
        else:
            result["errors"] += 1
        time.sleep(gap_s)

    return result


def interact_camera(serial: str, shots: int = 3, gap_s: float = 1.5) -> Dict:
    """Launch Google Camera, auto-locate the shutter button via uiautomator,
    and take N photos. Camera preview alone triggers LWIS DMA-BUF allocation
    via lwis_platform_dma_buffer_alloc → dma-heap.

    Returns dict with keys: shots, taken, method, errors.
    """
    result: Dict = {"shots": shots, "taken": 0, "method": "unknown", "errors": 0}

    adb_shell_cp(serial, "am force-stop com.google.android.GoogleCamera", timeout_s=8)
    time.sleep(0.5)

    cp = adb_shell_cp(
        serial,
        "am start -a android.media.action.STILL_IMAGE_CAMERA",
        timeout_s=15,
        check=False,
    )
    ok = cp.returncode == 0 and "Error:" not in (cp.stdout or "")
    if not ok:
        result["errors"] = 1
        result["launch_error"] = (cp.stdout or "")[:200]
        return result

    # Wait for UI, then dismiss any setup/permission dialogs that may
    # cover the shutter button (e.g. "保存位置信息" / "完成" dialogs).
    time.sleep(2.0)
    dismissed = interactive_click_loop(serial, match_texts=list(CONSENT_MATCH_TEXTS) + ["完成"],
                                       rounds=2, gap_s=0.8)
    result["dismissed_dialogs"] = dismissed

    # Wait for camera preview to start (preview stream triggers
    # lwis_platform_dma_buffer_alloc → dma-heap).
    time.sleep(1.5)

    root = dump_ui(serial)
    if root is not None:
        # Method 1: find shutter button by resource-id substring
        targets = find_element_by_resource_id(root, "shutter_button")
        if targets:
            cx, cy, rid = targets[0]
            result["method"] = f"resource_id({rid})"
            result["target"] = [cx, cy]
            for _ in range(shots):
                if _tap(serial, cx, cy):
                    result["taken"] += 1
                else:
                    result["errors"] += 1
                time.sleep(gap_s)
            return result

        # Method 2: find by content-desc matching shutter keywords
        parent_map = _build_parent_map(root)
        shutter_keywords = ["拍照", "快门", "shutter", "Shutter", "Photo", "photo"]
        targets = find_clickable_by_text(root, parent_map, shutter_keywords, ())
        if targets:
            cx, cy = targets[0][0], targets[0][1]
            result["method"] = f"content_desc({cx},{cy})"
            result["target"] = [cx, cy]
            for _ in range(shots):
                if _tap(serial, cx, cy):
                    result["taken"] += 1
                else:
                    result["errors"] += 1
                time.sleep(gap_s)
            return result

    # Method 3: fallback to KEYCODE_CAMERA
    result["method"] = "keyevent_camera"
    for _ in range(shots):
        cp2 = adb_shell_cp(serial, "input keyevent KEYCODE_CAMERA", timeout_s=5, check=False)
        if cp2.returncode == 0:
            result["taken"] += 1
        else:
            result["errors"] += 1
        time.sleep(gap_s)

    return result


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


INTERACTION_FUNCTIONS = {
    "douyin": interact_douyin,
    "camera": interact_camera,
}


def run_app_interaction(serial: str, pkg: str) -> Optional[Dict]:
    """Look up pkg in INTERACTION_MAP and run the matching interaction function.

    Returns the result dict, or None if no interaction is registered for this package.
    """
    interaction_name = INTERACTION_MAP.get(pkg)
    if interaction_name is None:
        return None
    func = INTERACTION_FUNCTIONS.get(interaction_name)
    if func is None:
        return None
    try:
        return func(serial)
    except Exception as e:
        return {"error": str(e)}
