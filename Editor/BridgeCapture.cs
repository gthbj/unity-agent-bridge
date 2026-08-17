using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering;
using Debug = UnityEngine.Debug;

namespace AgentBridge.Editor
{
    /// <summary>
    /// Cold-process batchmode capture: renders the running game (including ScreenSpaceOverlay
    /// canvases) to a PNG so a coding agent can look at it.
    ///
    /// Invoke:
    ///   Tuanjie -batchmode -projectPath X -executeMethod AgentBridge.Editor.BridgeCapture.Run
    ///           -abOut /tmp/shot.png [-abScene Assets/Scenes/Game.unity] [-abWidth 1080]
    ///           [-abHeight 1920] [-abSettleFrames 150] [-abTimeoutSec 180]
    ///           [-abSetupMethod Ns.Type.Method] [-abSetupArg value] [-abMinUniqueColors 8]
    ///           -logFile Y
    /// Never pass -nographics (Null device renders garbage without erroring) and never pass
    /// -quit (this method exits the editor itself with a meaningful code).
    ///
    /// Every rule in here is backed by a measured failure mode (Docs~/EVIDENCE.md):
    /// entering Play Mode domain-reloads and wipes statics, so all state lives in
    /// SessionState; WaitForEndOfFrame never completes in batchmode, so settling is
    /// frame-counted with plain yield return null; ScreenCapture.* silently does nothing,
    /// so we render through the camera; Screen is stuck at 640x480, so layout size comes
    /// from the camera's RenderTexture + explicit aspect.
    /// </summary>
    [InitializeOnLoad]
    public static class BridgeCapture
    {
        private const string Tag = "[AgentBridge]";
        private const string KeyPending = "AgentBridge.Pending";
        private const string KeyDeadline = "AgentBridge.DeadlineTicksUtc";
        private const string KeyOut = "AgentBridge.Out";
        private const string KeyScene = "AgentBridge.Scene";
        private const string KeyWidth = "AgentBridge.Width";
        private const string KeyHeight = "AgentBridge.Height";
        private const string KeySettle = "AgentBridge.Settle";
        private const string KeySetupMethod = "AgentBridge.SetupMethod";
        private const string KeySetupArg = "AgentBridge.SetupArg";
        private const string KeyMinColors = "AgentBridge.MinColors";

        // Exit codes (documented in README; the python client translates them).
        public const int ExitOk = 0;
        public const int ExitWatchdog = 3;
        public const int ExitBadArgs = 11;
        public const int ExitNullGraphics = 12;
        public const int ExitSceneMissing = 13;
        public const int ExitNoCamera = 14;
        public const int ExitWriteFailed = 15;
        public const int ExitTooFewColors = 16;
        public const int ExitSetupFailed = 17;
        public const int ExitRenderFailed = 18;

        static BridgeCapture()
        {
            // Re-arm after every domain reload (Play Mode entry recompiles the world).
            EditorApplication.playModeStateChanged -= OnPlayModeChanged;
            EditorApplication.playModeStateChanged += OnPlayModeChanged;
            if (SessionState.GetBool(KeyPending, false))
            {
                Debug.Log($"{Tag} re-armed after domain reload");
                EditorApplication.update -= Watchdog;
                EditorApplication.update += Watchdog;
            }
        }

        public static void Run()
        {
            var args = ParseArgs(Environment.GetCommandLineArgs());
            if (args == null)
            {
                Fail(ExitBadArgs, "missing required -abOut <path.png>");
                return;
            }

            Debug.Log($"{Tag} device={SystemInfo.graphicsDeviceType} name={SystemInfo.graphicsDeviceName}");
            if (SystemInfo.graphicsDeviceType == GraphicsDeviceType.Null)
            {
                // The -nographics trap: rendering "succeeds" but every pixel is garbage.
                Fail(ExitNullGraphics, "graphics device is Null (was -nographics passed?); captures would be silent garbage");
                return;
            }

            var scenePath = args.Scene ?? FirstEnabledBuildScene();
            if (string.IsNullOrEmpty(scenePath) || !File.Exists(scenePath))
            {
                Fail(ExitSceneMissing, $"scene not found: '{scenePath ?? "<none configured; pass -abScene>"}'");
                return;
            }

            try
            {
                var scene = EditorSceneManager.OpenScene(scenePath, OpenSceneMode.Single);
                Debug.Log($"{Tag} opened scene={scene.path} rootCount={scene.rootCount}");
            }
            catch (Exception e)
            {
                Fail(ExitSceneMissing, $"OpenScene failed: {e.GetType().Name}: {e.Message}");
                return;
            }

            // Persist everything the play-mode side needs: statics do not survive the reload.
            SessionState.SetBool(KeyPending, true);
            SessionState.SetString(KeyOut, args.Out);
            SessionState.SetString(KeyScene, scenePath);
            SessionState.SetInt(KeyWidth, args.Width);
            SessionState.SetInt(KeyHeight, args.Height);
            SessionState.SetInt(KeySettle, args.SettleFrames);
            SessionState.SetString(KeySetupMethod, args.SetupMethod ?? "");
            SessionState.SetString(KeySetupArg, args.SetupArg ?? "");
            SessionState.SetInt(KeyMinColors, args.MinUniqueColors);
            SessionState.SetString(KeyDeadline,
                DateTime.UtcNow.AddSeconds(args.TimeoutSec).Ticks.ToString(CultureInfo.InvariantCulture));

            EditorApplication.update -= Watchdog;
            EditorApplication.update += Watchdog;

            Debug.Log($"{Tag} entering play mode (settle={args.SettleFrames} frames, timeout={args.TimeoutSec}s)");
            EditorApplication.EnterPlaymode();
        }

        private static void OnPlayModeChanged(PlayModeStateChange change)
        {
            Debug.Log($"{Tag} playModeStateChanged={change}");
            if (change != PlayModeStateChange.EnteredPlayMode) return;
            if (!SessionState.GetBool(KeyPending, false)) return;

            var runner = new GameObject("AgentBridgeCaptureRunner");
            UnityEngine.Object.DontDestroyOnLoad(runner);
            var behaviour = runner.AddComponent<BridgeCaptureBehaviour>();
            behaviour.Configure(
                SessionState.GetString(KeyOut, ""),
                SessionState.GetInt(KeyWidth, 1080),
                SessionState.GetInt(KeyHeight, 1920),
                SessionState.GetInt(KeySettle, 150),
                SessionState.GetString(KeySetupMethod, ""),
                SessionState.GetString(KeySetupArg, ""),
                SessionState.GetInt(KeyMinColors, 8));
        }

        private static void Watchdog()
        {
            var raw = SessionState.GetString(KeyDeadline, "");
            if (string.IsNullOrEmpty(raw)) return;
            if (!long.TryParse(raw, NumberStyles.Integer, CultureInfo.InvariantCulture, out var ticks)) return;
            if (DateTime.UtcNow.Ticks <= ticks) return;
            // Hangs, not errors, are the dominant failure mode here (WaitForEndOfFrame,
            // un-re-armed reload). Convert them into a hard exit a caller can see.
            Fail(ExitWatchdog, "watchdog timeout");
        }

        internal static void Fail(int code, string reason)
        {
            Debug.Log($"{Tag} RESULT=FAIL code={code} reason={reason}");
            WriteResult(false, code, reason, null, 0);
            SessionState.SetBool(KeyPending, false);
            EditorApplication.Exit(code);
        }

        internal static void Succeed(string pngPath, int uniqueColors)
        {
            Debug.Log($"{Tag} RESULT=OK out={pngPath} uniqueColors={uniqueColors}");
            WriteResult(true, ExitOk, null, pngPath, uniqueColors);
            SessionState.SetBool(KeyPending, false);
            EditorApplication.Exit(ExitOk);
        }

        private static void WriteResult(bool ok, int code, string error, string pngPath, int uniqueColors)
        {
            var outPath = SessionState.GetString(KeyOut, "");
            if (string.IsNullOrEmpty(outPath)) return;
            try
            {
                var json = "{"
                    + $"\"ok\":{(ok ? "true" : "false")},\"exitCode\":{code}"
                    + $",\"width\":{SessionState.GetInt(KeyWidth, 0)},\"height\":{SessionState.GetInt(KeyHeight, 0)}"
                    + $",\"scene\":\"{Escape(SessionState.GetString(KeyScene, ""))}\""
                    + $",\"uniqueColors\":{uniqueColors}"
                    + $",\"graphicsDevice\":\"{SystemInfo.graphicsDeviceType}\""
                    + (pngPath != null ? $",\"png\":\"{Escape(pngPath)}\"" : "")
                    + (error != null ? $",\"error\":\"{Escape(error)}\"" : "")
                    + "}";
                File.WriteAllText(outPath + ".json", json);
            }
            catch (Exception e)
            {
                Debug.Log($"{Tag} result json write failed: {e.Message}");
            }
        }

        private static string Escape(string s)
            => s.Replace("\\", "\\\\").Replace("\"", "\\\"");

        private static string FirstEnabledBuildScene()
        {
            foreach (var s in EditorBuildSettings.scenes)
            {
                if (s.enabled) return s.path;
            }
            return null;
        }

        private sealed class Args
        {
            public string Out;
            public string Scene;
            public int Width = 1080;
            public int Height = 1920;
            public int SettleFrames = 150;
            public int TimeoutSec = 180;
            public string SetupMethod;
            public string SetupArg;
            public int MinUniqueColors = 8;
        }

        private static Args ParseArgs(string[] argv)
        {
            var a = new Args();
            for (var i = 0; i < argv.Length - 1; i++)
            {
                var v = argv[i + 1];
                switch (argv[i])
                {
                    case "-abOut": a.Out = v; break;
                    case "-abScene": a.Scene = v; break;
                    case "-abWidth": int.TryParse(v, out a.Width); break;
                    case "-abHeight": int.TryParse(v, out a.Height); break;
                    case "-abSettleFrames": int.TryParse(v, out a.SettleFrames); break;
                    case "-abTimeoutSec": int.TryParse(v, out a.TimeoutSec); break;
                    case "-abSetupMethod": a.SetupMethod = v; break;
                    case "-abSetupArg": a.SetupArg = v; break;
                    case "-abMinUniqueColors": int.TryParse(v, out a.MinUniqueColors); break;
                }
            }
            return string.IsNullOrEmpty(a.Out) ? null : a;
        }
    }

    /// <summary>
    /// Play-mode side. Settling is frame-counted with plain <c>yield return null</c> —
    /// never WaitForEndOfFrame / WaitForSeconds, which do not complete in batchmode.
    /// </summary>
    internal sealed class BridgeCaptureBehaviour : MonoBehaviour
    {
        private string _out;
        private int _width;
        private int _height;
        private int _settleFrames;
        private string _setupMethod;
        private string _setupArg;
        private int _minUniqueColors;

        public void Configure(string outPath, int width, int height, int settleFrames,
            string setupMethod, string setupArg, int minUniqueColors)
        {
            _out = outPath;
            _width = width;
            _height = height;
            _settleFrames = settleFrames;
            _setupMethod = setupMethod;
            _setupArg = setupArg;
            _minUniqueColors = minUniqueColors;
        }

        private IEnumerator Start()
        {
            for (var i = 0; i < _settleFrames; i++) yield return null;

            if (!string.IsNullOrEmpty(_setupMethod))
            {
                if (!InvokeSetup(_setupMethod, _setupArg, out var setupError))
                {
                    BridgeCapture.Fail(BridgeCapture.ExitSetupFailed, setupError);
                    yield break;
                }
                // Let whatever the hook changed (level load, theme swap) settle too.
                for (var i = 0; i < 60; i++) yield return null;
            }

            var cam = Camera.main;
            if (cam == null) cam = FindObjectOfType<Camera>(true);
            var temporaryCamera = false;
            if (cam == null)
            {
                // Overlay-only UI scenes (e.g. a main menu) legitimately have no camera:
                // ScreenSpaceOverlay needs none. Provide one just for the shot; a
                // full-screen UI background covers its clear color anyway.
                var canvasCount = FindObjectsOfType<Canvas>(true).Length;
                if (canvasCount == 0)
                {
                    BridgeCapture.Fail(BridgeCapture.ExitNoCamera, "no Camera and no Canvas after settle; nothing to capture");
                    yield break;
                }
                var go = new GameObject("AgentBridgeTempCamera");
                DontDestroyOnLoad(go);
                cam = go.AddComponent<Camera>();
                cam.orthographic = true;
                cam.clearFlags = CameraClearFlags.SolidColor;
                cam.backgroundColor = Color.clear;
                // Full mask: ScreenSpaceCamera canvas geometry IS subject to camera culling
                // (unlike overlay), so mask 0 would blank the very UI we came to shoot.
                cam.cullingMask = ~0;
                temporaryCamera = true;
            }

            var canvases = FindObjectsOfType<Canvas>(true);
            Debug.Log($"[AgentBridge] camera={cam.name}{(temporaryCamera ? " (temporary)" : "")} canvases={canvases.Length} screen={Screen.width}x{Screen.height}");

            var tx = new CaptureTransaction(cam, canvases, _width, _height);
            byte[] png = null;
            int uniqueColors = 0;
            string renderError = null;
            try
            {
                tx.Apply();
                Canvas.ForceUpdateCanvases();
                // Real frames so uGUI lays out against the new display size.
                for (var i = 0; i < 10; i++) yield return null;
                foreach (var c in canvases)
                {
                    if (c == null) continue;
                    Debug.Log($"[AgentBridge]   canvas '{c.name}' mode={c.renderMode} order={c.sortingOrder} " +
                              $"active={c.isActiveAndEnabled} children={c.transform.childCount} " +
                              $"display={c.renderingDisplaySize} scale={c.scaleFactor}");
                    for (var ci = 0; ci < c.transform.childCount; ci++)
                    {
                        var ch = c.transform.GetChild(ci);
                        Debug.Log($"[AgentBridge]     child '{ch.name}' active={ch.gameObject.activeInHierarchy} kids={ch.childCount}");
                    }
                }
                try
                {
                    png = tx.Render(out uniqueColors);
                    Debug.Log($"[AgentBridge] midPixel={tx.LastMidPixel} uniqueColors={uniqueColors}");
                }
                catch (Exception e)
                {
                    renderError = $"{e.GetType().Name}: {e.Message}";
                }
            }
            finally
            {
                tx.Restore();
            }

            if (png == null)
            {
                BridgeCapture.Fail(BridgeCapture.ExitRenderFailed, renderError ?? "render produced no data");
                yield break;
            }
            if (uniqueColors < _minUniqueColors)
            {
                BridgeCapture.Fail(BridgeCapture.ExitTooFewColors,
                    $"only {uniqueColors} unique colors (< {_minUniqueColors}); capture is likely a blank/garbage frame");
                yield break;
            }

            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(_out)) ?? ".");
                File.WriteAllBytes(_out, png);
            }
            catch (Exception e)
            {
                BridgeCapture.Fail(BridgeCapture.ExitWriteFailed, $"write failed: {e.Message}");
                yield break;
            }

            Destroy(gameObject);
            BridgeCapture.Succeed(_out, uniqueColors);
        }

        private static bool InvokeSetup(string qualified, string arg, out string error)
        {
            error = null;
            var dot = qualified.LastIndexOf('.');
            if (dot <= 0)
            {
                error = $"setup method '{qualified}' is not Type.Method";
                return false;
            }
            var typeName = qualified.Substring(0, dot);
            var methodName = qualified.Substring(dot + 1);
            Type type = null;
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                type = asm.GetType(typeName);
                if (type != null) break;
            }
            if (type == null)
            {
                error = $"setup type '{typeName}' not found";
                return false;
            }
            const BindingFlags flags = BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;
            try
            {
                var withArg = type.GetMethod(methodName, flags, null, new[] { typeof(string) }, null);
                if (withArg != null)
                {
                    withArg.Invoke(null, new object[] { arg });
                    return true;
                }
                var noArg = type.GetMethod(methodName, flags, null, Type.EmptyTypes, null);
                if (noArg != null)
                {
                    noArg.Invoke(null, null);
                    return true;
                }
                error = $"static '{methodName}()' / '{methodName}(string)' not found on {typeName}";
                return false;
            }
            catch (Exception e)
            {
                var inner = e is TargetInvocationException tie && tie.InnerException != null ? tie.InnerException : e;
                error = $"setup threw {inner.GetType().Name}: {inner.Message}";
                return false;
            }
        }
    }

    /// <summary>
    /// The overlay-canvas capture transaction. ScreenSpaceOverlay canvases are invisible to
    /// camera rendering, so we flip them to ScreenSpaceCamera for the shot; layout size and
    /// aspect come from the camera's RenderTexture, sidestepping the frozen 640x480 Screen.
    /// Snapshot in ctor, mutate in Apply, put Restore in a finally — this runs against the
    /// live scene, and a half-restored scene poisons everything after it.
    /// </summary>
    internal sealed class CaptureTransaction
    {
        private readonly Camera _cam;
        private readonly Canvas[] _canvases;
        private readonly int _width;
        private readonly int _height;

        private readonly RenderMode[] _savedModes;
        private readonly Camera[] _savedCams;
        private readonly float[] _savedPlaneDistances;
        private readonly RenderTexture _savedTarget;
        private readonly float _savedAspect;

        private RenderTexture _rt;

        /// <summary>Center pixel of the last render, for blank-frame diagnostics.</summary>
        public Color32 LastMidPixel { get; private set; }

        public CaptureTransaction(Camera cam, Canvas[] canvases, int width, int height)
        {
            _cam = cam;
            _canvases = canvases;
            _width = width;
            _height = height;
            _savedModes = new RenderMode[canvases.Length];
            _savedCams = new Camera[canvases.Length];
            _savedPlaneDistances = new float[canvases.Length];
            for (var i = 0; i < canvases.Length; i++)
            {
                _savedModes[i] = canvases[i].renderMode;
                _savedCams[i] = canvases[i].worldCamera;
                _savedPlaneDistances[i] = canvases[i].planeDistance;
            }
            _savedTarget = cam.targetTexture;
            _savedAspect = cam.aspect;
        }

        public void Apply()
        {
            _rt = new RenderTexture(_width, _height, 24, RenderTextureFormat.ARGB32);
            _rt.Create();
            _cam.targetTexture = _rt;
            _cam.aspect = _width / (float)_height;
            for (var i = 0; i < _canvases.Length; i++)
            {
                var c = _canvases[i];
                if (c == null) continue;
                // Only overlay canvases need the flip; camera/world canvases already render.
                if (_savedModes[i] != RenderMode.ScreenSpaceOverlay) continue;
                c.renderMode = RenderMode.ScreenSpaceCamera;
                c.worldCamera = _cam;
                c.planeDistance = 1f;
            }
        }

        public byte[] Render(out int uniqueColors)
        {
            var prevActive = RenderTexture.active;
            try
            {
                _cam.Render();
                RenderTexture.active = _rt;
                var tex = new Texture2D(_width, _height, TextureFormat.RGBA32, false);
                tex.ReadPixels(new Rect(0, 0, _width, _height), 0, 0);
                tex.Apply();
                LastMidPixel = tex.GetPixel(_width / 2, _height / 2);
                uniqueColors = CountUniqueColors(tex);
                var png = tex.EncodeToPNG();
                UnityEngine.Object.Destroy(tex);
                return png;
            }
            finally
            {
                RenderTexture.active = prevActive;
            }
        }

        public void Restore()
        {
            try
            {
                for (var i = 0; i < _canvases.Length; i++)
                {
                    if (_canvases[i] == null) continue;
                    _canvases[i].renderMode = _savedModes[i];
                    _canvases[i].worldCamera = _savedCams[i];
                    _canvases[i].planeDistance = _savedPlaneDistances[i];
                }
                if (_cam != null)
                {
                    _cam.targetTexture = _savedTarget;
                    _cam.aspect = _savedAspect;
                }
            }
            catch (Exception e)
            {
                Debug.Log($"[AgentBridge] restore failed (cold process exits anyway): {e.Message}");
            }
            finally
            {
                if (_rt != null)
                {
                    _rt.Release();
                    UnityEngine.Object.Destroy(_rt);
                    _rt = null;
                }
            }
        }

        private static int CountUniqueColors(Texture2D tex)
        {
            var pixels = tex.GetPixels32();
            var seen = new HashSet<int>();
            // Stride keeps this O(50k) at 1080x1920; we only need "not a blank frame".
            const int stride = 7;
            for (var i = 0; i < pixels.Length; i += stride)
            {
                var p = pixels[i];
                seen.Add((p.r << 24) | (p.g << 16) | (p.b << 8) | p.a);
                if (seen.Count > 4096) break;
            }
            return seen.Count;
        }
    }
}
