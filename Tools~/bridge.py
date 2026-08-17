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
import subprocess
import sys
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


def capture(args: argparse.Namespace) -> int:
    project = os.path.abspath(args.project)
    out = os.path.abspath(args.out)
    editor = find_editor(project, args.editor)
    check_package(project)

    log = args.log or os.path.join(tempfile.gettempdir(), "agent_bridge_capture.log")
    cmd = [
        editor,
        "-batchmode",  # deliberately NOT -nographics: capture needs a real GPU
        "-projectPath", project,
        "-executeMethod", "AgentBridge.Editor.BridgeCapture.Run",
        "-abOut", out,
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
    # Outer timeout: editor-side watchdog plus cold-start / import headroom.
    outer = args.timeout + 300
    try:
        proc = subprocess.run(cmd, timeout=outer, check=False)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        print(f"[bridge] FAIL: no exit within {outer}s (outer timeout); editor killed")
        return 4

    meaning = EXIT_MEANINGS.get(code, "unknown")
    result_path = out + ".json"
    if os.path.isfile(result_path):
        try:
            with open(result_path, encoding="utf-8") as fh:
                print(f"[bridge] result : {fh.read().strip()}")
        except OSError:
            pass

    if code == 0 and os.path.isfile(out):
        print(f"[bridge] OK: {out}")
        return 0
    if code == 0:
        # Belt and braces: a zero exit without a file is exactly the silent-failure
        # class the editor side is built to prevent. Never report it as success.
        print(f"[bridge] FAIL: editor exited 0 but {out} does not exist")
        return 5

    print(f"[bridge] FAIL: exit {code} ({meaning})")
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
