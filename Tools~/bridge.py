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
import zlib

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


PINNED_HINT = (
    f'  "{PKG_NAME}": "git+ssh://git@github.com/gthbj/unity-agent-bridge.git#<40-hex-sha>"'
)


def check_package(project: str) -> None:
    """Require the host to depend on a SHA-pinned git package.

    A `file:` local path is deliberately rejected: the lock records only the path,
    never the content, so the same host commit silently changes behaviour as this
    repo moves -- and the host's own gates never re-run because it has no diff.
    """
    manifest = os.path.join(project, "Packages", "manifest.json")
    try:
        with open(manifest, encoding="utf-8") as fh:
            deps = json.load(fh).get("dependencies", {})
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"cannot read {manifest}: {e}")
    if PKG_NAME not in deps:
        sys.exit(f"{PKG_NAME} is not in {manifest}.\n"
                 f"Add it yourself (this client never edits your manifest):\n{PINNED_HINT}")

    dep = str(deps[PKG_NAME])
    if dep.startswith("file:"):
        sys.exit(f"{PKG_NAME} is pinned as a local path ({dep}).\n"
                 f"Local paths are a mutable source: the lock records the path, not the\n"
                 f"content, so this host commit's behaviour drifts with the tool repo and\n"
                 f"its gates never re-run. Use a SHA-pinned git URL instead:\n{PINNED_HINT}")
    frag = dep.partition("#")[2]
    if not SHA40.match(frag):
        sys.exit(f"{PKG_NAME} must be pinned to a full 40-hex commit SHA; got {dep!r}.\n"
                 f"{PINNED_HINT}")


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def validate_png(path: str, expect_w: int, expect_h: int):
    """Walk the whole chunk stream. Checking only the first 24 bytes let a
    truncated 24-byte stub pass as a real capture -- the same silent false-green
    this tool exists to kill, reintroduced inside its own fix."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        return None, f"unreadable ({e})"
    if not data.startswith(PNG_MAGIC):
        return None, "missing PNG signature"

    pos = len(PNG_MAGIC)
    dims = None
    seen_idat = seen_iend = False
    first = True
    idat = bytearray()
    ihdr_body = b""
    while pos < len(data):
        if pos + 8 > len(data):
            return None, f"truncated chunk header at byte {pos}"
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        body_end = pos + 8 + length
        if body_end + 4 > len(data):
            return None, f"truncated {ctype.decode('latin1', 'replace')} chunk at byte {pos}"
        body = data[pos + 8:body_end]
        want_crc = int.from_bytes(data[body_end:body_end + 4], "big")
        if zlib.crc32(ctype + body) & 0xFFFFFFFF != want_crc:
            return None, f"bad CRC on {ctype.decode('latin1', 'replace')} chunk at byte {pos}"
        if first:
            if ctype != b"IHDR" or length != 13:
                return None, "first chunk is not a 13-byte IHDR"
            dims = (int.from_bytes(body[0:4], "big"), int.from_bytes(body[4:8], "big"))
            ihdr_body = body
            first = False
        elif ctype == b"IDAT":
            seen_idat = True
            idat += body
        pos = body_end + 4
        if ctype == b"IEND":
            seen_iend = True
            break

    if not seen_iend:
        return None, "no IEND chunk"
    if pos != len(data):
        return None, f"{len(data) - pos} trailing bytes after IEND"
    if not seen_idat:
        return None, "no IDAT chunk"
    if dims != (expect_w, expect_h):
        return None, f"PNG is {dims[0]}x{dims[1]}, expected {expect_w}x{expect_h}"

    # Chunk framing being intact says nothing about the pixels. An empty or
    # truncated-but-correctly-CRC'd IDAT is a structurally perfect, undecodable image.
    depth, color, comp, filt, interlace = ihdr_body[8:13]
    if (depth, color, comp, filt, interlace) != (8, 6, 0, 0, 0):
        return None, (f"unsupported IHDR (depth={depth} color={color} comp={comp} "
                      f"filter={filt} interlace={interlace}); expected 8-bit RGBA, non-interlaced")
    try:
        d = zlib.decompressobj()
        raw = d.decompress(bytes(idat))
        raw += d.flush()
    except zlib.error as e:
        return None, f"IDAT is not a decodable zlib stream ({e})"
    if not d.eof:
        return None, "IDAT zlib stream is incomplete or truncated"
    if d.unused_data:
        return None, f"{len(d.unused_data)} bytes of trailing data after the IDAT zlib stream"
    expect_len = expect_h * (1 + expect_w * 4)
    if len(raw) != expect_len:
        return None, f"decoded {len(raw)} bytes, expected {expect_len} for {expect_w}x{expect_h} RGBA"
    return dims, ""


def capture(args: argparse.Namespace) -> int:
    project = os.path.abspath(args.project)
    out = os.path.abspath(args.out)
    editor = find_editor(project, args.editor)
    check_package(project)

    log = args.log or os.path.join(tempfile.gettempdir(), "agent_bridge_capture.log")
    out_dir = os.path.dirname(out) or "."
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        print(f"[bridge] FAIL: cannot create output directory {out_dir}: {e}")
        return 15

    # Serialize on --out so two concurrent captures cannot interleave and publish
    # one call's PNG next to the other call's JSON.
    lock_path = out + ".lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(lock_fd, f"pid={os.getpid()} started={time.time():.0f}\n".encode())
        os.close(lock_fd)
    except FileExistsError:
        print(f"[bridge] FAIL: {lock_path} exists -- another capture is writing {out}. "
              f"If no capture is running, delete it.")
        return 7
    except OSError as e:
        print(f"[bridge] FAIL: cannot create lock {lock_path}: {e}")
        return 7

    # Stage INSIDE the destination directory so the final publish is a same-filesystem
    # rename. A system-temp staging dir makes any cross-volume --out fail with EXDEV.
    try:
        workdir = tempfile.mkdtemp(prefix=".agent-bridge-", dir=out_dir)
    except OSError as e:
        os.unlink(lock_path)
        print(f"[bridge] FAIL: cannot stage next to {out}: {e}")
        return 15

    try:
        return _capture_locked(args, project, out, editor, log, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        try:
            os.unlink(lock_path)
        except OSError:
            pass


def _capture_locked(args, project: str, out: str, editor: str, log: str, workdir: str) -> int:
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
        return report_untouched(4, out)
    except OSError as e:
        print(f"[bridge] FAIL: could not run editor: {e}")
        return report_untouched(6, out)

    ok, reason, result = validate_run(code, staged, args)
    if not ok:
        print(f"[bridge] FAIL: {reason} (editor exit {code}: {EXIT_MEANINGS.get(code, 'unknown')})")
        tail_log(log)
        return report_untouched(code if code != 0 else 5, out)

    # The editor recorded its own staging path; the caller only ever sees --out.
    result["png"] = out
    staged_json = staged + ".json"
    try:
        with open(staged_json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False)
    except OSError as e:
        print(f"[bridge] FAIL: could not rewrite result JSON: {e}")
        return report_untouched(15, out)

    # Two renames cannot be one transaction, so publish the PNG first: it is the
    # artifact the caller actually looks at. Dying between the two then leaves this
    # run's real image with lagging metadata -- never fresh metadata pointing at a
    # stale image, which is the failure this tool exists to prevent.
    try:
        os.replace(staged, out)
    except OSError as e:
        print(f"[bridge] FAIL: could not publish PNG: {e}")
        return report_untouched(15, out)

    dims, why = validate_png(out, args.width, args.height)
    if dims is None:
        # Never leave a file that failed validation sitting at --out looking like a result.
        print(f"[bridge] FAIL: published file failed post-publish validation: {why}")
        for stale in (out, out + ".json"):
            try:
                os.unlink(stale)
            except OSError:
                pass
        print(f"[bridge] NOTE: removed {out} and any stale sidecar; nothing to read here.")
        return 5

    try:
        os.replace(staged_json, out + ".json")
    except OSError as e:
        print(f"[bridge] WARN: PNG published but sidecar JSON was not: {e}")
        try:
            os.unlink(out + ".json")
        except OSError:
            pass
    print(f"[bridge] OK: {out} ({dims[0]}x{dims[1]})")
    return 0


def validate_run(code: int, staged: str, args):
    """Success means THIS call produced a structurally valid PNG matching the request."""
    if code != 0:
        return False, "editor reported failure", None

    result_path = staged + ".json"
    if not os.path.isfile(result_path):
        return False, "editor exited 0 but wrote no result JSON", None
    try:
        with open(result_path, encoding="utf-8") as fh:
            result = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return False, f"result JSON unreadable ({e})", None

    print(f"[bridge] result : {json.dumps(result, ensure_ascii=False)}")
    if result.get("ok") is not True or result.get("exitCode") != 0:
        return False, "result JSON does not report success", None
    if result.get("width") != args.width or result.get("height") != args.height:
        return False, (f"result JSON size {result.get('width')}x{result.get('height')} "
                       f"!= requested {args.width}x{args.height}"), None
    if args.scene and result.get("scene") != args.scene:
        return False, f"result JSON scene {result.get('scene')!r} != requested {args.scene!r}", None
    if os.path.abspath(result.get("png") or "") != os.path.abspath(staged):
        return False, (f"result JSON png {result.get('png')!r} is not this call's staged "
                       f"path {staged!r}"), None
    if not os.path.isfile(staged):
        return False, "result JSON claims success but no PNG was written", None

    dims, why = validate_png(staged, args.width, args.height)
    if dims is None:
        return False, f"staged PNG rejected: {why}", None
    return True, "", result


def report_untouched(code: int, out: str) -> int:
    """Nothing was published. Make any pre-existing --out impossible to mistake for fresh."""
    if os.path.exists(out):
        try:
            mtime = os.path.getmtime(out)
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
            mins = (time.time() - mtime) / 60
            age = f"{when} ({mins:.0f} min ago)"
        except OSError:
            age = "unknown time"
        print(f"[bridge] NOTE: {out} was NOT updated. It is a PREVIOUS capture from {age} "
              f"-- do not read it as this run's result.")
    return code


def tail_log(log: str, want: int = 15) -> None:
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            lines = [l.rstrip() for l in fh if "[AgentBridge]" in l]
    except OSError:
        return
    for line in lines[-want:]:
        print(f"  {line}")


def build_bad_pngs(w: int, h: int):
    """The malformed shapes a 24-byte prefix check waved through."""
    def chunk(ctype: bytes, body: bytes) -> bytes:
        return (len(body).to_bytes(4, "big") + ctype + body
                + (zlib.crc32(ctype + body) & 0xFFFFFFFF).to_bytes(4, "big"))

    ihdr_body = w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])
    ihdr = chunk(b"IHDR", ihdr_body)
    raw = b"".join(b"\x00" + b"\x00\x00\x00\xff" * w for _ in range(h))
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    good = PNG_MAGIC + ihdr + idat + iend
    bad_crc = bytearray(good)
    bad_crc[-5] ^= 0xFF
    empty_idat = chunk(b"IDAT", b"")
    short_idat = chunk(b"IDAT", zlib.compress(raw)[:-1])
    return {
        "empty": b"",
        "empty IDAT (chunk-valid, undecodable)": PNG_MAGIC + ihdr + empty_idat + iend,
        "truncated zlib tail in IDAT": PNG_MAGIC + ihdr + short_idat + iend,
        "IDAT decodes short (wrong pixel count)":
            PNG_MAGIC + ihdr + chunk(b"IDAT", zlib.compress(raw[: len(raw) // 2])) + iend,
        "16-bit depth (unsupported IHDR)": PNG_MAGIC + chunk(
            b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes([16, 6, 0, 0, 0])
        ) + idat + iend,
        "24-byte stub (the exact reported bypass)":
            PNG_MAGIC + (13).to_bytes(4, "big") + b"IHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big"),
        "truncated after IHDR": PNG_MAGIC + ihdr,
        "no IDAT": PNG_MAGIC + ihdr + iend,
        "no IEND": PNG_MAGIC + ihdr + idat,
        "trailing bytes after IEND": good + b"junk",
        "bad CRC": bytes(bad_crc),
        "wrong dimensions": PNG_MAGIC + chunk(
            b"IHDR", (w + 1).to_bytes(4, "big") + h.to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])
        ) + idat + iend,
    }, good


def selftest(_args) -> int:
    """Runnable regression for validate_png. No editor, no Unity required."""
    w, h = 8, 4
    bad, good = build_bad_pngs(w, h)
    failures = 0
    with tempfile.TemporaryDirectory() as td:
        for name, blob in bad.items():
            path = os.path.join(td, "x.png")
            with open(path, "wb") as fh:
                fh.write(blob)
            dims, why = validate_png(path, w, h)
            if dims is not None:
                print(f"  FAIL rejected-expected but accepted: {name}")
                failures += 1
            else:
                print(f"  ok   rejected {name}: {why}")
        path = os.path.join(td, "good.png")
        with open(path, "wb") as fh:
            fh.write(good)
        dims, why = validate_png(path, w, h)
        if dims != (w, h):
            print(f"  FAIL valid PNG rejected: {why}")
            failures += 1
        else:
            print("  ok   accepted a structurally valid PNG")
    print(f"selftest: {failures} failure(s)")
    return 1 if failures else 0


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
    sub.add_parser("selftest", help="run the validate_png regression (no Unity needed)")
    args = top.parse_args()
    if args.cmd == "capture":
        sys.exit(capture(args))
    if args.cmd == "selftest":
        sys.exit(selftest(args))


if __name__ == "__main__":
    main()
