# Unity Agent Bridge

给 coding agent 用的 Unity / 团结引擎批处理工具。v0.1 只做一件事：**冷进程批处理截图**——把真实运行中的游戏画面（含 `ScreenSpaceOverlay` UI）渲染成 PNG，agent 自己看。

## 为什么存在

批处理 Unity 的失败形态大多是**静默出错或挂死，不是报错**：`-nographics` 渲染出垃圾像素但不抛异常；`ScreenCapture` 两个 API 静默无产出；`WaitForEndOfFrame` 永不返回；进 Play Mode 的域重载吞掉所有 static 状态。本包把这些坑逐条转成硬断言和非零退出码。完整实测证据（S1–S13）见 `Docs~/EVIDENCE.md`。

## 已验证边界（诚实声明）

| 维度 | 已验证 | 未验证 |
|---|---|---|
| 引擎 | **Tuanjie 2022.3.62t10**（macOS / Metal / Apple Silicon） | 原生 Unity、其它版本、其它平台 |
| 渲染管线 | **内置管线** | URP / HDRP（`cam.Render()` 语义不同，大概率要另写路径） |
| UI | **uGUI ScreenSpaceOverlay / Camera** | UI Toolkit runtime |

名字叫 unity-agent-bridge 是奔着通用去的，但**声明只覆盖上表左列**。三个实际消费者（arrows / water_sort / meowdoku）都在左列内。

## 用法

1. 目标项目 `Packages/manifest.json` 加依赖（本工具**不会**替你改 manifest）：

```json
"com.gthbj.agent-bridge": "file:/ABSOLUTE/PATH/unity-agent-bridge"
```

2. 截图：

```bash
python3 Tools~/bridge.py capture --project /path/to/project --out /tmp/shot.png
```

不传 `--scene` 时用 Build Settings 里第一个启用场景。`--setup-method Ns.Type.StaticMethod`（可带 `--setup-arg`）在开拍前调一个项目侧静态钩子（选关、换主题等），钩子由各项目自己提供。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功，PNG 已写出 |
| 3 | 看门狗超时（编辑器侧挂死被转成退出） |
| 11–18 | 参数 / Null 设备 / 场景缺失 / 无相机 / 写盘失败 / 颜色数过低（疑似空图）/ 钩子失败 / 渲染失败 |
| 134 | 项目被另一个编辑器实例占用（Unity 按项目路径单实例） |
| 199 | 许可 socket 被挡（沙箱网络策略），发生在一切执行之前 |

## 红线

- 截图只用于 **agent 自查与回归发现**；「好不好看、收不收」的视觉验收仍由 owner 在 GUI 拍板。
- 本包产物不得作为任何项目的合并门禁证据。
- 一律**不传 `-nographics`**（会拿到 Null 设备静默出垃圾），也不传 `-quit`（编辑器侧自己带退出码退出）。

## 实测验证（2026-08-17，三消费者）

| 项目 | 场景 | 结果 |
|---|---|---|
| arrows | Game.unity | ✅ 完整画面（HUD / 三心 / 棋盘 / 底部按钮，727 色） |
| arrows | MainMenu.unity | ✅ 完整画面（纯 canvas UI，460 色） |
| water_sort | GameScene.unity（默认） | ✅ 开屏 logo（4097 色封顶；拍玩法画面需加大 `--settle-frames`） |
| meowdoku | Main.unity（默认） | ⚠️ 只出背景色，嵌套内容缺失（见下） |

**已知局限（meowdoku 形态）**：UI 全部由自定义 `Graphic`（`OnPopulateMesh` 程序化网格）构成时，
canvas 背景 Image 正常渲染但嵌套程序化内容不出，四条路径（临时相机、拉长 settle、原生 640x480、
正常循环读回）均未解决，根因未定位。颜色门（exit 16）会把这类帧拦下来而不是当成功交付——
这正是它存在的目的。遇到时先用 `--settle-frames` 排除时序，再怀疑 UI 栈本身。

**arrows Boot 场景提示**：经 Boot 引导拍菜单会拍到卡在纯色 `ScreenTransition` 的帧（exit 16）。
直接 `--scene Assets/Scenes/MainMenu.unity` 即可。

## Roadmap

- **常驻编辑器服务**（消 11s 冷启动、`dev-test` / `invoke`）：设计已完成并通过一轮评审（7 P1 + 2 P2 全部闭合），见 arrows 仓 PR #74 的 PRD r2；等冷启动真正成为瓶颈时按该设计实现，届时把 arrows 专属的门禁隔离条款换成各项目自己的。
