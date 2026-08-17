#!/usr/bin/env python3
"""unity-agent-bridge client.

Drives a cold batchmode Unity/Tuanjie editor to capture a real rendered frame of the
running game (including ScreenSpaceOverlay UI) as a PNG, so a coding agent can look
at it.

Usage:
  python3 bridge.py capture --project /path/to/project --out /tmp/shot.png
      [--scene Assets/Scenes/Game.unity] [--width 1080] [--height 1920]
      [--settle-frames 150] [--timeout 180] [--setup-method Ns.Type.Method]
      [--setup-arg value] [--min-unique-colors 8] [--editor /path/to/editor]

The target project must have the com.gthbj.agent-bridge package in its
Packages/manifest.json — this client refuses to edit other projects' manifests.

Stdlib only. Exit code mirrors the editor-side result (see README).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tempfile

PKG_NAME = "com.gthbj.agent-bridge"

EXIT_MEANINGS = {
    0: "ok",
    3: "watchdog timeout (editor-side hang converted to exit)",
    11: "bad arguments",
    12: "graphics device is Null — -nographics must NOT be used for capture",
    13: "scene missing (pass --scene, or enable one in Build Settings)",
    14: "no Camera in scene after settle",
    15: "PNG write failed",
    16: "too few unique colors — capture is likely a blank/garbage frame",
    17: "setup method failed",
    18: "render failed",
    134: "another editor instance has this project open (Unity is single-instance per project path)",
    199: "license channel timeout before anything ran (sandbox blocking the license socket?)",
}


def find_editor(project: str, override: str) -> str:
    if override:
        if not os.path.isfile(override):
            sys.exit(f"--editor not found: {override}")
        return override
    pv = os.path.join(project, "ProjectSettings", "ProjectVersion.txt")
    try:
        with open(pv, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        sys.exit(f"not a Unity project (no ProjectVersion.txt): {project}")
    m = re.search(r"m_EditorVersion:\s*(\S+)", text)
    if not m:
        sys.exit(f"cannot parse editor version from {pv}")
    version = m.group(1)
    candidates = [
        f"/Applications/Tuanjie/Hub/Editor/{version}/Tuanjie.app/Contents/MacOS/Tuanjie",
        f"/Applications/Unity/Hub/Editor/{version}/Unity.app/Contents/MacOS/Unity",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    sys.exit(
        f"editor {version} not installed; looked at:\n  " + "\n  ".join(candidates)
        + "\npass --editor explicitly if it lives elsewhere"
    )


def check_package(project: str) -> None:
    manifest = os.path.join(project, "Packages", "manifest.json")
    try:
        with open(manifest, encoding="utf-8") as fh:
            deps = json.load(fh).get("dependencies", {})
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"cannot read {manifest}: {e}")
    if PKG_NAME not in deps:
        sys.exit(
            f"{PKG_NAME} is not in {manifest}.\n"
            f'Add it yourself (this client never edits your manifest):\n'
            f'  "{PKG_NAME}": "file:/ABSOLUTE/PATH/TO/unity-agent-bridge"\n'
            f'  or a git URL once the repo is pushed.'
        )


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_dimensions(path: str):
    """Return (w, h) from the IHDR chunk, or None if this is not a usable PNG."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or not head.startswith(PNG_MAGIC) or head[12:16] != b"IHDR":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def capture(args: argparse.Namespace) -> int:
    project = os.path.abspath(args.project)
    out = os.path.abspath(args.out)
    editor = find_editor(project, args.editor)
    check_package(project)

    log = args.log or os.path.join(tempfile.gettempdir(), "agent_bridge_capture.log")

    # Everything the editor writes goes to a fresh directory owned by THIS call.
    # Reusing --out directly is how a previous run's PNG gets reported as this
    # run's success -- the exact silent false-green this tool exists to prevent.
    workdir = tempfile.mkdtemp(prefix="agent-bridge-")
    staged = os.path.join(workdir, "capture.png")

    cmd = [
        editor,
        "-batchmode",  # deliberately NOT -nographics: capture needs a real GPU
        "-projectPath", project,
        "-executeMethod", "AgentBridge.Editor.BridgeCapture.Run",
        "-abOut", staged,
        "-abWidth", str(args.width),
        "-abHeight", str(args.height),
        "-abSettleFrames", str(args.settle_frames),
        "-abTimeoutSec", str(args.timeout),
        "-abMinUniqueColors", str(args.min_unique_colors),
        "-logFile", log,
    ]
    if args.scene:
        cmd += ["-abScene", args.scene]
    if args.setup_method:
        cmd += ["-abSetupMethod", args.setup_method]
        if args.setup_arg is not None:
            cmd += ["-abSetupArg", args.setup_arg]

    print(f"[bridge] editor : {editor}")
    print(f"[bridge] project: {project}")
    print(f"[bridge] log    : {log}")
    outer = args.timeout + 300
    try:
        code = subprocess.run(cmd, timeout=outer, check=False).returncode
    except subprocess.TimeoutExpired:
        print(f"[bridge] FAIL: no exit within {outer}s (outer timeout); editor killed")
        return finish_failure(4, out, workdir, log, tail=False)
    except OSError as e:
        print(f"[bridge] FAIL: could not run editor: {e}")
        return finish_failure(6, out, workdir, log, tail=False)

    ok, reason = validate_run(code, staged, args)
    if not ok:
        meaning = EXIT_MEANINGS.get(code, "unknown")
        print(f"[bridge] FAIL: {reason} (editor exit {code}: {meaning})")
        return finish_failure(code if code != 0 else 5, out, workdir, log)

    try:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        os.replace(staged, out)
        result_src = staged + ".json"
        if os.path.isfile(result_src):
            os.replace(result_src, out + ".json")
    except OSError as e:
        print(f"[bridge] FAIL: could not move capture into place: {e}")
        return finish_failure(15, out, workdir, log, tail=False)

    shutil.rmtree(workdir, ignore_errors=True)
    w, h = png_dimensions(out)
    print(f"[bridge] OK: {out} ({w}x{h})")
    return 0


def validate_run(code: int, staged: str, args: argparse.Namespace):
    """A run counts as successful only if THIS call produced a matching PNG."""
    if code != 0:
        return False, "editor reported failure"

    result_path = staged + ".json"
    if not os.path.isfile(result_path):
        return False, "editor exited 0 but wrote no result JSON"
    try:
        with open(result_path, encoding="utf-8") as fh:
            result = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return False, f"result JSON unreadable ({e})"

    print(f"[bridge] result : {json.dumps(result, ensure_ascii=False)}")
    if result.get("ok") is not True or result.get("exitCode") != 0:
        return False, "result JSON does not report success"
    if result.get("width") != args.width or result.get("height") != args.height:
        return False, (f"result JSON size {result.get('width')}x{result.get('height')} "
                       f"!= requested {args.width}x{args.height}")
    if args.scene and result.get("scene") != args.scene:
        return False, f"result JSON scene {result.get('scene')!r} != requested {args.scene!r}"

    if not os.path.isfile(staged):
        return False, "result JSON claims success but no PNG was written"
    dims = png_dimensions(staged)
    if dims is None:
        return False, "staged file is not a valid PNG"
    if dims != (args.width, args.height):
        return False, f"PNG is {dims[0]}x{dims[1]}, expected {args.width}x{args.height}"
    return True, ""


def finish_failure(code: int, out: str, workdir: str, log: str, tail: bool = True) -> int:
    """Clean up this call's artifacts and make any stale --out impossible to mistake."""
    shutil.rmtree(workdir, ignore_errors=True)
    if os.path.exists(out):
        try:
            age = time.time() - os.path.getmtime(out)
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(out)))
        except OSError:
            age, when = -1, "unknown"
        print(f"[bridge] NOTE: {out} was NOT updated. It is a PREVIOUS capture "
              f"from {when} ({age / 60:.0f} min ago) -- do not read it as this run's result.")
    if tail:
        tail_log(log)
    return code


def tail_log(log: str, want: int = 15) -> None:
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            lines = [l.rstrip() for l in fh if "[AgentBridge]" in l]
    except OSError:
        return
    for line in lines[-want:]:
        print(f"  {line}")


def main() -> None:
    top = argparse.ArgumentParser(prog="bridge.py")
    sub = top.add_subparsers(dest="cmd", required=True)
    cap = sub.add_parser("capture", help="render the running game to a PNG")
    cap.add_argument("--project", required=True)
    cap.add_argument("--out", required=True)
    cap.add_argument("--scene")
    cap.add_argument("--width", type=int, default=1080)
    cap.add_argument("--height", type=int, default=1920)
    cap.add_argument("--settle-frames", type=int, default=150)
    cap.add_argument("--timeout", type=int, default=180)
    cap.add_argument("--setup-method")
    cap.add_argument("--setup-arg")
    cap.add_argument("--min-unique-colors", type=int, default=8)
    cap.add_argument("--editor")
    cap.add_argument("--log")
    args = top.parse_args()
    if args.cmd == "capture":
        sys.exit(capture(args))


if __name__ == "__main__":
    main()
