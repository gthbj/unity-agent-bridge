# 实测证据（S1–S13）

> 2026-08-17，本机 Tuanjie 2022.3.62t10 / macOS / Apple M1 Pro（Metal）。
> 移植自 arrows 仓 PRD_20260817_02 r2 §3；原文含完整上下文与设计推导（见同目录 DESIGN_ORIGIN 文件）。

立项不靠推测。以下每条都是本机 Tuanjie 2022.3.62t10 实跑结论：

| # | 结论 | 证据 |
|---|---|---|
| S1 | `-batchmode` **不加** `-nographics` 时有真实 GPU：`graphicsDeviceType=Metal` / `Apple M1 Pro` | 渲染像素等于请求的清屏色 `RGBA(0.149,0.349,0.749)` |
| S2 | 🔴 `-nographics` 是 `Null Device`，渲染**不报错但出垃圾像素** `RGBA(0.804,…)` | 同一段代码两种模式对比 |
| S3 | 批处理进程**不带 `-quit` 会一直活着**，`EditorApplication.update` 约 260 tick/秒 | `ping` 在 2.3s 内累计 607 tick |
| S4 | `TestRunnerApi` 可在常驻进程内反复跑 EditMode：368 selected / 349 passed / 0 failed / **20.6s** | 无冷启动开销 |
| S5 | `Filter.categoryNames = ["!Slow"]` 与 CLI `-testCategory '!Slow'` **是同一条代码路径** | `SettingsBuilder.cs:65-68` 把 `-testCategory` 原样塞进 `Filter.categoryNames`；`RuntimeTestRunnerFilter.cs:68-69` 把 `!` 前缀转成 `NotFilter` |
| S6 | 常驻进程可进出 Play Mode 并**跨域重载存活**（`[InitializeOnLoad]` + `SessionState` 重挂），pid 不变 | Play Mode 往返后 `ping` 仍答（tick 49486） |
| S7 | 🔴 进 Play Mode 会**域重载**，普通 `static` 字段与 `EditorApplication.update` 委托全丢 | 未按 S6 重挂的探针**挂死、零日志、不退出** |
| S8 | 🔴 Play Mode 播的是**当前打开的场景**；不显式开 `Game.unity` 就拍到 Unity 默认天空盒 | 首个探针产出天空盒 PNG |
| S9 | 🔴 批处理下 `Screen` 恒为 **640x480**；`-screen-width/-screen-height` 对编辑器**无效**，`Screen.SetResolution` 也**无效** | 两者设置后 Play Mode 仍 640x480 |
| S10 | 🔴 `WaitForEndOfFrame` 在批处理里**永不返回**，协程就地挂死 | 探针卡在该 yield，最终由看门狗 `exit 3` |
| S11 | 🔴 `ScreenCapture.CaptureScreenshotAsTexture()` **返回 null**、`ScreenCapture.CaptureScreenshot(path)` **不写文件**，两者都**不抛异常** | 等 40 帧后文件仍不存在 |
| S12 | ✅ overlay canvas 切 `ScreenSpaceCamera` + 相机渲进 RT 可拍到**完整画面**；且 `Canvas.renderingDisplaySize` 跟随相机 `pixelRect`，故设 `targetTexture` + `aspect` 即可得**正确竖屏排版** | 实测产出 1080x1920 竖屏图，HUD / 心 / 棋盘 / 底部按钮齐全 |
| S13 | ✅ 看门狗能把「挂死」转成非零退出 | S7/S10 两次挂死均由看门狗 `exit 3` 收场 |

**S2 / S7 / S8 / S10 / S11 是同一类东西：失败形态是「静默出错或挂死」，不是报错。** 它们在下文逐条对应一个硬断言，这是本需求的主要工程价值。


## 对本包实现的直接约束

- S2 ⟹ capture 启动即断言 `graphicsDeviceType != Null`，否则退出码 12。
- S7 ⟹ 全部跨 Play Mode 状态走 `SessionState`，`[InitializeOnLoad]` 重挂；绝不依赖 static 字段存活。
- S8 ⟹ 必须显式打开目标场景后再 EnterPlaymode。
- S9 ⟹ 分辨率来自相机 `targetTexture` + 显式 `aspect`，不碰 `Screen`。
- S10 ⟹ settle 只用逐帧 `yield return null` 计数，代码里禁止任何 `WaitFor*`。
- S11 ⟹ 不使用 `ScreenCapture.*`；唯一路径是 overlay canvas 切 `ScreenSpaceCamera` + 相机渲 RT。
- S13 ⟹ 看门狗把挂死转成非零退出（本包退出码 3）。
