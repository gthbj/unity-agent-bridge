# PRD_20260817_02：Editor 常驻服务与批处理视觉自检

> 文档维护：Claude Opus 5（2026-08-17；r2 处置 Codex 评审 7 P1 + 2 P2，见 §12）

## 1. 背景

owner 2026-08-17 问「本机有没有 unity cli、对 agent 是不是更方便」，确认现状后拍板做本需求。

先澄清一个容易误解的前提：**Unity / 团结引擎从来没有独立的 `unity` CLI 二进制**，编辑器可执行文件本身就是 CLI，`-batchmode` 即命令行模式。本项目的测试、包门、关卡表生成与 APK 构建**已经全部走 CLI**（`README.md` 四条命令 + `tools/build_android.sh`），GUI 只承担 owner 的 Game View 视觉验收。所以本需求不是「引入 CLI」，而是消除现有 CLI 用法的三个结构性成本：

- **每条命令付一次冷启动**。冷启动约 11s（`README.md` 基准），快车道测试本体约 20s（本次实测 349 passed / 20.6s）。修复—重测循环里这是约 35% 的纯开销。
- **每个操作都要先加一个 `static` 入口**。想问「这关的 `BoardSizing.Calculate` 返回什么」，当前得写方法、编译、`-executeMethod`、再等 11s。
- **agent 拿不到画面**。视觉侧此前只能做数值代理（`preferredWidth`、TextGenerator 墨迹几何）。

## 2. 目标 / 非目标

### 目标

- **G1** 常驻编辑器服务：长活 `-batchmode` 进程，按文件投递协议接命令，命令间不再付冷启动。
- **G2** `tools/arrows_editor.py` 客户端：`start / stop / status / dev-test / invoke / capture / logs`。
- **G3** 批处理截图：Play Mode 下渲染真实游戏画面（含 uGUI 叠加层）落 PNG。
- **G4** 零运行时足迹：全部代码 Editor-only，Player 程序集与构建产物不受影响。

### 非目标

- **不改任何现有门禁**。`AGENTS.md` §七 判定与 `README.md` 四条冷启动命令原样保留。
- **不做 owner 的视觉验收**（见 §7.1）。
- **不引入第三方包**（含社区 Unity MCP）。`KNOWN_CONSTRAINTS.md` 要求第三方包须 owner 批准；本需求自建，不触发该条。
- 不做 golden 图比对（后续需求，v1 只出图）。

## 3. 实测依据（立项前 spike，本机 2026-08-17）

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

## 4. 架构

### 4.1 服务端（Editor-only）

新增 `Assets/Scripts/Editor/Daemon/`，并**新增一个 Editor-only asmdef `Arrows.EditorDaemon`**（`includePlatforms: [Editor]`）。

🔴 **不能沿用 `Assembly-CSharp-Editor`**（r2 修正）：asmdef 程序集**不能引用预定义程序集** `Assembly-CSharp-Editor`（预定义程序集在 asmdef 之后编译）。而 `Assets/Tests/EditMode/Arrows.Tests.EditMode.asmdef` 是 asmdef，若 daemon 落在预定义程序集里，§10 要求的协议往返 / 陈旧判定 / 空闲计算单测**根本引用不到真实类型**，只能退化成源码字符串检查或复制一份逻辑——复制的测试守不住生产实现。故 daemon 必须自带 asmdef，并让 `Arrows.Tests.EditMode.asmdef` 显式引用它。这不要求 `Arrows.Core` 依赖 `UnityEngine`，也不产生 Player 足迹。

- `EditorDaemon.cs`：`[InitializeOnLoad]` 生命周期、跨域重载重挂（S6/S7）、tick 轮询、请求分发、空闲超时。
- `DaemonProtocol.cs`：请求 / 响应 DTO 与 `JsonUtility` 序列化。
- `Commands/*.cs`：每命令一个文件。

### 4.2 传输：文件投递，不是 socket

```
<project>/Temp/ArrowsDaemon/     # Temp/ 已在 .gitignore
  daemon.json                     # pid / projectPath / 引擎版本 / graphicsDeviceType / startedAt / idleDeadline / busy
  req/<id>.json                   # 客户端写请求
  res/<id>.json                   # 服务端写响应
  log/<id>.log                    # 该命令期间捕获的 Unity 日志
```

🔴 **选文件而不是 socket 有实测理由**：`CLAUDE.md`「操作要点」第 7 条已记录，seatbelt 把 `connect()` 当**网络**权限管，团结引擎许可 socket 正因此在沙箱里被挡、每条命令在跑测试**之前** `exit 199`。Codex 在 `-p arrows-unity` profile 下工作，走文件协议整类绕开该失败面。

两个方向都必须**先写 `.tmp` 再 `rename()`**（APFS 同目录 rename 原子），避免读到半截 JSON。

🔴 **但原子写不等于恰好一次（r2 新增）**：`rename` 只保证 JSON 不被读半截，不保证请求只执行一次。必须另外定义**所有权协议**：

- **实例身份**：每次启动生成 boot UUID（`daemonInstanceId`）；`daemon.json`、每个请求与响应都带 `protocolVersion` + `daemonInstanceId` + `requestId`。`daemon.json` 自身也走原子写。
- **认领点唯一**：请求 id 不可预测，客户端以**排他创建**写入 `req/`；服务端把 `req/<id>.json` **原子 rename 到 `inflight/<id>.json`** 作为**唯一认领动作**。认领成功才执行，终态响应**只写一次**。
- **状态机**：`Idle / Claimed / Busy / Stopping / Dead`，单一串行 worker。
- **陈旧判定**：不得只验 pid 存活（pid 会复用）。必须**同时**核对 `daemonInstanceId`、pid、进程启动时间或命令行、项目路径，四者任一不符即判陈旧。
- **空闲计时只在 `Idle` 生效**，进入终态后重置；同一 tick 内**已认领请求优先于 idle shutdown**。
- **`start` 竞态**：客户端侧用原子 lock 文件，避免两个 `start` 同时拉起编辑器争项目锁。
- **`stop` 忙时语义**：等待或拒绝须明确，并提供超时后的 `--force` 恢复路径（客户端超时只停止等待，**解不开一个永久 busy 的项目锁**）。
- **客户端退出码**：`ok:false`、测试失败、超时、daemon 消失一律**非零退出**。

### 4.3 客户端

`tools/arrows_editor.py`（与既有 `tools/pick_*.py` 同风格，纯标准库）：

| 子命令 | 行为 |
|---|---|
| `start` | 拉起常驻进程；已在跑则报告并退出 0 |
| `stop` | 优雅关闭；进程已死则清理 spool |
| `status` | pid / 项目路径 / 运行时长 / 空闲剩余 / graphics 模式 / 是否忙 |
| `dev-test` | 跑 EditMode（r2 改名，见 §8），透传 `--category` / `--assembly` / `--name` |
| `invoke` | 调**白名单内**的查询型静态方法（r2 收窄，见 §5.1） |
| `capture` | 见 §7 |
| `logs` | 拉某次请求的日志 |

## 5. 命令集与协议

请求 / 响应均为单个 JSON 对象。响应**必有** `ok`、`command`、`elapsedMs`；失败**必有** `error`（类型 + 消息）。

| 命令 | 关键参数 | 响应要点 |
|---|---|---|
| `ping` | — | `uptimeMs`、`busy`、`graphicsDeviceType` |
| `status` | — | `daemon.json` 全量 + 当前请求 |
| `refresh` | — | `AssetDatabase.Refresh()` 后的编译错误列表（有错**原样回传**，不得吞） |
| `dev-test` | `categories[]`、`assemblies[]`、`namePattern`（**无 `resultsPath`**，见 §8） | 全部终态计数、失败用例名与消息、NUnit XML 路径（固定在实例目录内）、`gateEligible:false` |
| `invoke` | `name`（白名单键）、强类型参数 | 返回值 / 异常，及期间日志 |
| `capture` | 见 §7.4 | PNG 路径、实际分辨率、`graphicsDeviceType`、`uniqueColorCount` |
| `shutdown` | — | 应答后退出 |

🔴 `dev-test` 的类别过滤**必须直接透传给 `Filter.categoryNames`**，不得自己解析 `!` 前缀——按 S5，CLI 与 API 共用同一实现，自己解析等于制造第二份口径。

### 5.1 🔴 `invoke` 是白名单，不是任意反射（r2 收窄）

原稿承诺「调任意 `static` 方法、覆盖现有全部 `-executeMethod` 入口」，**这个承诺兑现不了，而且会从结构上击穿 §8**：

- 现有入口读的是**不可变的** `Environment.GetCommandLineArgs()`（`LevelPackGate.cs:35`、`RectLevelTableGenerator.cs:18/25`），反射传 `args[]` **根本到不了它们手里**，只会静默取到 daemon 自己的启动参数。
- 它们会调 `EditorApplication.Exit`（`LevelPackGate.cs:124`、`RectLevelTableGenerator.cs:30/63/78/99`）——`invoke` 一下就**把常驻进程杀了**。
- `BuildScript` 还要求进程启动时带 `-buildTarget Android`，且 `ProjectSettings.asset` 的快照恢复由 `tools/build_android.sh` 外层负责，daemon 里调等于绕过构建契约。
- 最严重的是：`invoke Arrows.Editor.LevelPackGate.RunFullValidation` 会产出**不带 `gateEligible:false` 的门禁级结果**，红线当场失效。

故 v1 定义为**显式注册的白名单**：每个可调命令在代码里注册名字、强类型参数与返回序列化，只收**查询型 / 可回滚**操作。**硬拒绝**：包门、关卡表生成提升、Android 构建、任何调用 `EditorApplication.Exit` / `AssetDatabase.SaveAssets` 的入口。README 里那几条冷启动入口**继续只能冷跑**。

## 6. 生命周期与故障模式（每条对应一个实测教训）

- 🔴 **每条命令都必须应答，失败路径也应答**。spike 里 `play` 忘了写响应，驱动脚本直接干等到超时。客户端每个请求必须带**超时**，服务端每个分支必须有**终态响应**。
- 🔴 **跨域重载必须是可恢复状态机，不能一律报错（r2 重写）**：原稿写「重载时正在飞的请求一律回 `domain-reloaded`」，但这与 §5/§7 自相矛盾——`capture` 按 S7 进入 Play Mode **必然**域重载，`refresh` 触发脚本编译时也会；照原规则这两条命令永远走不到成功终态。只重挂 `EditorApplication.update` 能让 daemon 活着，**恢复不了请求的阶段、回调与回滚快照**，结果是请求被中止、重复执行、或永远没有响应。

  正确做法：把 `daemonInstanceId + requestId + phase` **持久化到磁盘 `inflight` 记录**（不能只放 `SessionState`），并在 `[InitializeOnLoad]`、`playModeStateChanged`、编译完成回调里**续跑**。各命令的阶段必须写死：
  - `capture`：`OpeningScene → EnteringPlay → Settling → Rendering → Restoring → ExitingPlay → Responded`
  - `refresh`：`Refreshing → Compiling → Responded`

  只有**非预期**重载（阶段表里没有该转移）才转成 `domain-reloaded` 错误，且**先回滚再写唯一终态响应**。
- **空闲自杀**：默认 `--idle-timeout 1800`s。单实例锁按项目路径生效，被遗忘的常驻进程会让该工作树后续所有 batchmode `exit 134`（`CLAUDE.md` 第 8 条）。
- 🔴 **看门狗**：任何进 Play Mode 的命令必须挂超时看门狗并强制 `EditorApplication.Exit(非零)`。S7/S10 的失败形态是**挂死不是报错**，只等进程退出的调用方会一直等下去；S13 已证明看门狗能兜住。
- **陈旧 spool**：客户端读 `daemon.json` 先验 pid 存活，死了就清理并如实报告，不得沿用。
- 🔴 **`ProjectSettings.asset` 必须逐命令核对，不能等到 `stop`（r2 加严）**：`KNOWN_CONSTRAINTS` 已记录批处理会把空图标槽写回该文件，而 `tools/pick_test_lane.py` 会因此**误报 android 档**。常驻进程一活就是半小时，只在 `stop` 警告意味着这期间**每一次档位判定都看到伪改动**，调用方可能已据脏 diff 做了错误决策（本次立项 spike 就真的弄脏了一次）。

  做法：每条可能触发写回的命令**前后各记一次文件 hash/字节快照**并立即在响应里报 `knownBatchmodeDirty`；仅当确认当前内容就是本命令产生的已知改写、且期间没有并发的人工编辑时**恢复原字节**，否则 fail-loud 要求人工处理。`status` 持续暴露该状态。A6 以**恢复后**的 diff 为验收证据。
- **不隐式 `SaveAssets()`**。

## 7. 视觉自检（capture）

### 7.1 定位（先说清楚不做什么）

`TODO.md` 已立规矩：**「agent 不代做视觉验收」**。本命令**不改这条**。它的用途是 agent 自查——「这次改动把 HUD 撞歪了吗」「换主题后结算页还对吗」——属于**回归发现**。「好不好看、收不收」仍只由 owner 在 GUI 拍板。实现方不得把 capture 写进任何流程去替代 owner 验收。

### 7.2 唯一可行的截图路径

本项目 4 个 Canvas（`HUD` / `BoardViewportMask` / `WellDoneSettlementView` / `GameOverView`）**全是 `ScreenSpaceOverlay`**，而按 S11，`ScreenCapture` 两个 API 在批处理里都静默失败。唯一可行路径：

**临时把 overlay canvas 切成 `ScreenSpaceCamera` → 相机 `Render()` 进 RenderTexture → `ReadPixels` → 无论成败都还原 `renderMode` 与 `worldCamera`**。

🔴 **capture 必须定义成事务，`finally` 不够（r2 重写）**：原稿只要求还原 Canvas 的 `renderMode`/`worldCamera`，但这条流程还会改 `Camera.targetTexture`、`Camera.aspect`、`RenderTexture.active`，创建 RT / Texture，切场景，进出 Play Mode，换 level 与 theme。而且普通 C# `finally` **跨不过域重载，也跨不过 `EditorApplication.Exit`**——恰恰是本命令必然经历的两件事。

必须列出**全量快照与后置条件**并逐项还原：

| 类别 | 快照项 |
|---|---|
| 场景 | 原打开场景集合、active scene、dirty 状态 |
| Play Mode | 进入前是否 playing |
| 每个参与 Canvas | `renderMode`、`worldCamera`、`planeDistance`、`sortingOrder` |
| 相机 | `targetTexture`、`aspect`、`rect`、`enabled` |
| 全局 | 原 `RenderTexture.active` |
| 会话 | 主题、level / session 状态 |
| 临时对象 | RT / Texture2D / runner GameObject 全部释放 |

🔴 **主题必须走非持久化路径**：`GameSession.SetThemeId` → `SaveService` 会 `PlayerPrefs.Save()`（`SaveService.cs:63-64`），一次截图就把主题**永久写进存档**、越出本次命令影响后续运行。`--theme` 只能走 `Theme.SetActiveTheme(id)` 后再 `LoadLevel`，**禁止写 save**。

成功、断言异常、看门狗触发、`shutdown`、非预期域重载**必须进入同一条 rollback**；回滚阶段本身要持久化（见 §6）。返回前必须确认已回到 EditMode，并验证 `ping` → `dev-test` 仍可用、无 dirty scene / asset / PlayerPrefs 变化。

### 7.3 分辨率（S9 的解法）

`Screen` 压不动，但**不需要压**：

- `cam.targetTexture = rt(1080x1920)` 使 `cam.pixelWidth/Height` 变为 1080x1920；
- `Canvas.renderingDisplaySize` 跟随相机 pixelRect（实测 `(1080.00, 1920.00)`），故 `CanvasScaler.ScaleWithScreenSize`（参考分辨率 1080x1920）按竖屏排版；
- 🔴 还须**显式设 `cam.aspect`**：`BoardCameraController.cs:909` 优先取 `controlledCamera.aspect`，只有它无效才回落 `Screen.width/height`——不设就会拿到 4:3 的棋盘构图。

三者齐备时实测产出与上线一致的 1080x1920 竖屏画面。

### 7.4 参数与断言

参数：`--scene`（默认 `Assets/Scenes/Game.unity`）、`--level`、`--theme`、`--width/--height`（默认 1080x1920）、`--settle-frames`（默认 150）、`--out`。

`--level` 调 `GameController.LoadLevel(int)`（`GameController.cs:123`，public），**不需要改任何运行时代码**。

🔴 必须**失败退出**的断言（每条对应一个实测坑）：

| 断言 | 对应 |
|---|---|
| `SystemInfo.graphicsDeviceType != Null` | S2 静默垃圾像素 |
| 目标场景已显式打开，且 `GameController` 存在 | S8 天空盒 |
| settle 只能由 `EditorApplication.update` 帧状态机推进；**禁止创建任何 `WaitFor*`**，并用源码 / 程序集契约测试守住 | S10 挂死（r2：原稿写成「代码路径中不出现」，那是源码约束不是运行时断言，无法执行） |
| 冻结采样步长、阈值与读取色彩格式后的 `uniqueColorCount` | 防「纯色空图」——spike 里三个采样点恰好全落背景，差点误判成功 |
| **结构断言**（r2 新增，仅色彩数不够）：目标 Canvas / Camera 均 enabled；实际 pixel rect 等于请求值；HUD、棋盘、底部两个按钮对象存在，且其 RectTransform 与相机视口有**非零交集** | 只渲出背景渐变也能轻易过色彩门 |

### 7.5 验收

- 产出 1080x1920 PNG，肉眼可见 HUD 标题 / 心数 / 棋盘 / 底部两个圆形按钮。
- `-nographics` 下必须**失败退出**而非出图。
- 场景或 `GameController` 缺失必须失败。
- 异常路径下 canvas `renderMode` 仍被还原（用一次注入异常验证）。

## 8. 🔴 红线：常驻服务不得成为合并门禁

**常驻服务只是开发加速器，不是门。** 合并前判定仍**只**认 `AGENTS.md` §七 触发面 + `README.md` 冷启动命令。理由：同一进程里反复跑测试，静态状态可能跨轮泄漏，产生「常驻绿、冷跑红」；一个会在这种情况下放行的门比没有门更糟。

🔴 **原稿的三条措施都只是提示性约束，不是结构隔离（r2 重写）**：`gateEligible:false` 只活在响应 JSON 里，调用方直接拿 NUnit XML 就绕过了；末行警告在脚本消费 / 日志截断 / 只复制通过数时就消失；`pick_test_lane.py` 只选档位、**不验证 XML 是冷进程还是 daemon 产出**，所以「零改动」防不住误用；任意 `resultsPath` 甚至能覆盖 README 冷跑用的结果文件。同一份 resident XML 外观上可与冷跑证据**完全相同**，红线只能靠人记住，无法审计。

改为结构隔离：

1. **产物强制隔离**：resident 结果只能写 `Temp/ArrowsDaemon/<instanceId>/<requestId>/`，**取消 `resultsPath` 参数**，禁止写任何外部或 canonical 结果路径。
2. **双处打标**：`gateEligible=false`、`runnerMode=daemon`、`daemonInstanceId`、本实例内的 run 序号，**同时**写进响应 JSON **和 NUnit XML 的 `properties`**——让证据在 XML 层面就自证来源。
3. **命令改名** `test` → `dev-test`，客户端保留末行警告（作为辅助而非主要手段）。
4. **契约测试**扫描 `README.md` / `AGENTS.md` / `tools/pick_test_lane.py` / `tools/build_android.sh` **不得出现** `dev-test` 或 daemon 客户端调用。
5. **`invoke` 白名单硬拒绝全部门禁入口**（§5.1）。
6. `tools/pick_test_lane.py` 仍**零改动**——判定入口保持唯一。

> 若将来需要机器可验的门禁 provenance，正确做法是另加一个**只会启动新进程**的 cold wrapper，而不是让 daemon 产出同形态证据。

## 9. 验收标准

- **A1** `start → status → dev-test → stop` 全程可用；`status` 如实报告 pid / 项目路径 / graphics 模式。
- **A2** 🔴 **结果保真（r2 重写为可执行流程）**。原稿「一次 resident 对一次 cold 比通过/失败名集合」有三个洞：覆盖不到**第二轮才开始的泄漏**；丢掉 skipped / inconclusive / not-runnable / suite 级失败与重复参数化 case；没规定取 `name` 还是 `fullname`。两边**同时少选**同一批测试仍会「相等」。

  冻结流程：同一 clean commit、同一完整 filter，跑**一次新进程 cold `C1`**，再在**同一个 daemon 里连续跑 `D1`、`D2`**。从每份 NUnit XML 取 `test-case@fullname` 加参数构成 **multiset**（不去重），比较：
  - discovery 总数；
  - `Passed / Failed / Skipped / Inconclusive / NotRunnable` **全部终态**；
  - suite 级 error **单列**。

  要求 **`C1 = D1 = D2`**。原始 XML、log 与机器生成的 diff 入 PR。

  不等时：**保留差异，不得以 flaky 为由排除任何 case**；按差异 case 交替跑 fresh-cold / fresh-daemon 首轮 / 同 daemon 次轮来定位是泄漏、顺序依赖还是既有不稳定。**若 cold 自身就不稳定，A2 仍判失败**——先修那个测试再重跑，不能靠删出集合放行。

  **这条不过则本需求不可合并。**
- **A3** 提速可量化：常驻第二次起的快车道墙钟显著低于冷启动，PR comment 记录两组实测。
- **A4** 域重载存活：改一个 Editor 脚本触发重编译后，`ping` 仍答且 pid 不变。
- **A5** 空闲超时到点自杀并在日志写明原因。
- **A6** 🔴 **零运行时足迹**：`Arrows.Core` / `Arrows.Game` / `Arrows.LevelGen` 与 `ProjectSettings/**` **零改动**（以 §6 恢复后的 `git diff --stat` 为证）；`AndroidBuildContractTests` 仍绿。**允许的新增差异**（r2）：新的 Editor-only `Arrows.EditorDaemon.asmdef`，以及 `Arrows.Tests.EditMode.asmdef` 增加对它的引用。
- **A7** §7.5 全部通过。
- **A8** 现有 EditMode 全绿（档位按 `tools/pick_test_lane.py` 判定，本 PR 预期不落在慢测依赖面）。

## 10. 测试计划

- 新增 EditMode 单测覆盖**纯逻辑部分**：协议编解码往返、请求 id 生成与排他创建、陈旧判定（UUID / pid / 启动时间 / 项目路径四因子）、空闲超时计算、状态机转移表、`gateEligible` 恒 false、`invoke` 白名单拒绝门禁入口。必须是不依赖常驻进程的纯函数测试，并**通过 asmdef 引用真实类型**（见 §4.1），不得复制实现。
- 新增**契约测试**：`README.md` / `AGENTS.md` / `tools/pick_test_lane.py` / `tools/build_android.sh` 不得出现 `dev-test` 或 daemon 客户端调用（§8 第 4 条）；capture 实现不得创建 `WaitFor*`（§7.4）。
- 常驻进程端到端行为（A1/A2/A4）用 `tools/` 下脚本化冒烟覆盖，结果贴 PR comment；**不进 EditMode 套件**（避免测试里再起一个编辑器）。
- 🔴 A2 是本需求的核心信任证据：必须真跑两遍，并把两份测试名集合的 diff（应为空）贴进 PR。

## 11. 风险与备选

| 风险 | 处置 |
|---|---|
| 常驻进程静态状态泄漏导致结果失真 | §8 红线 + A2 集合比对；不一致就如实报告，**不得放宽 A2** |
| 遗忘的常驻进程占住工作树锁 | 空闲自杀 + `status` + `stop`；且常驻进程只在**工作树**跑，owner GUI 在主检出，路径不同锁不同 |
| Play Mode 挂死（S7/S10 形态） | 看门狗强制非零退出（S13 已验证） |
| `-nographics` 静默出垃圾图（S2） | capture 启动即断言 `graphicsDeviceType != Null` |
| 拍到错场景（S8 形态） | 显式开场景 + 断言 `GameController` 存在 |
| canvas 还原失败污染后续命令 | `finally` 还原 + 注入异常的验收用例 |

**备选方案（已否决）**：引入社区 Unity MCP 包——`KNOWN_CONSTRAINTS` 默认不引第三方包，且这些包针对 Unity 2021+/6，与团结引擎 2022.3 分支兼容性未知；自建服务端代码量可控且完全在既有约定内。


## 12. 评审处置（r2）

Codex 于 PR #74 提出 7 条 P1 阻塞 + 2 条 P2。**全部采纳，无回退**。其中两条可查证的事实已由 Claude 独立复核确认（不采信自述）：

- `LevelPackGate.cs:35` 读 `Environment.GetCommandLineArgs()`、`:124` 调 `EditorApplication.Exit(1)`；`RectLevelTableGenerator.cs:18/25` 与 `:30/63/78/99` 同形态 ⟹ 任意 `invoke` 既传不进参数、又会杀死常驻进程。**属实**。
- `GameSession.SetThemeId` → `SaveService.cs:63-64` 的 `PlayerPrefs.Save()` ⟹ `--theme` 会持久化主题。**属实**。

| 发现 | 处置 | 落点 |
|---|---|---|
| P1 域重载规则与 capture/refresh 自相矛盾 | 采纳，改为持久化阶段的可恢复状态机 | §6 |
| P1 任意 `invoke` 不可兑现且绕过 §8 | 采纳，收窄为白名单 + 硬拒绝门禁入口 | §5.1 |
| P1 只有原子写、无请求所有权与单实例状态机 | 采纳，补认领协议 / 实例 UUID / 四因子陈旧判定 / 空闲与 stop 语义 | §4.2 |
| P1 capture 回滚集合不完整且 `finally` 跨不过重载 | 采纳，改为事务 + 全量快照表 + 主题非持久化路径 | §7.2 |
| P1 §8 三条是提示性不是结构隔离 | 采纳，改为产物隔离 + XML 打标 + 改名 `dev-test` + 契约测试 | §8 |
| P1 A2 测不到跨轮泄漏、口径不可复现 | 采纳，冻结 `C1/D1/D2` multiset 全终态流程 | A2 |
| P1 asmdef 选择使 §10 单测无法编译 | 采纳，新增 `Arrows.EditorDaemon.asmdef` | §4.1、A6 |
| P2 §7.4 两条断言不可执行 | 采纳，改为帧状态机 + 契约测试 + 冻结阈值 + 结构断言 | §7.4 |
| P2 `ProjectSettings.asset` 只在 stop 警告 | 采纳，改为逐命令 hash 核对 + `status` 暴露 | §6 |
