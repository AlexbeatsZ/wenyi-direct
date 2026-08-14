# Kamyi

[English](README.md) | [简体中文](README.zh-CN.md)

Kamyi 是一个以章节为单位、可恢复执行的文学翻译流水线，支持 EPUB、FB2、
TXT、Markdown、HTML、PDF，以及项目定义的 JSON 交换格式。它先直接翻译完整
章节，再检查事实忠实度和中文阅读体验，只有通过验证的译文才会进入最终输出。

Kamyi 受到 [Wenyi](https://github.com/BigDawnGhost/wenyi) 项目的启发。本仓库只
包含可复用的翻译程序、测试、设计文档和脱敏示例配置，不包含书籍原文、生成的
汉化文件、运行状态或 API 密钥。

## 工作流程

Kamyi 会依次处理每个章节：

1. 直接翻译源文并写入可恢复的 Shadow 章节；
2. 进行源文可见的事实审校，只修复经过确认的因果范围；
3. 让中文阅读审校只查看读者可见的中文，再用源文验证需要修复的内容；
4. 所有质量门通过后，将章节原子地提升为 Formal 正式译文。

它不要求预先扫描全书，也不会依赖未来章节。模型只能写入当前阶段明确授权的
稳定段落 ID。

## 安装

Windows PowerShell：

```powershell
git clone https://github.com/AlexbeatsZ/kamyi.git
cd kamyi
uv sync
uv run kamyi --help
```

`uv` 会自动创建并使用项目环境，不需要全局安装 Python 包，也不会修改系统环境。

## 创建配置

创建当前书籍的项目配置和用户级模型目录：

```powershell
uv run kamyi init-config config.yaml
uv run kamyi models path
uv run kamyi models list
```

`config.yaml` 保存当前书籍的语言、上下文窗口、流水线策略、状态目录和输出目录。
可复用的供应商连接与模型选择保存在 `models path` 输出的位置；Windows 默认是
`%APPDATA%\kamyi\models.yaml`。

也可以从仓库中的脱敏示例开始：

```powershell
Copy-Item config.example.yaml config.yaml
New-Item -ItemType Directory -Force "$env:APPDATA\kamyi" | Out-Null
Copy-Item models.example.yaml "$env:APPDATA\kamyi\models.yaml"
```

`init-config` 不会覆盖已经存在的文件。请勿提交 `config.yaml`、`models.yaml`、
书籍运行状态或生成的译文。

## API 密钥与模型供应商

真实 API 密钥不应写入项目 YAML、源码、README、测试或 Git。模型目录只记录环境
变量的名称，例如 `api_key_env: DEEPSEEK_API_KEY`。

在运行 Kamyi 的 PowerShell 会话中设置对应变量：

```powershell
$env:DEEPSEEK_API_KEY = '替换为你自己的密钥'
uv run kamyi models list
uv run kamyi use translate deepseek-api deepseek-v4-flash
```

不要把真实密钥直接写进命令历史或仓库文件。`codex-cli` 和 `agy` 路由使用各自
CLI 已有的本地认证。OpenAI 兼容接口、Anthropic 兼容接口、Codex CLI 和 Agy 的
配置示例见 [docs/providers.md](docs/providers.md)。

## 翻译书籍

普通翻译命令可以随时中断并再次执行；已经完成的阶段不会重复调用模型：

```powershell
uv run kamyi translate path\to\book.epub --config config.yaml
```

双通道模式会让第 N 章的下游审校与第 N+1 章的上游工作重叠：

```powershell
uv run kamyi translate path\to\book.epub --parallel --config config.yaml
```

使用 `--chapters 0,2-4` 可以只处理指定章节。单次模型覆盖不会修改配置文件：

```powershell
uv run kamyi translate path\to\book.epub `
  --model translate=deepseek-api/deepseek-v4-flash `
  --model repair=codex/gpt-5.6-sol-high `
  --config config.yaml
```

## 重新审校已有正式译文

`review` 会从已有 Formal 译文开始一轮可恢复的事实审校和中文阅读审校，不会重新
进行初次翻译：

```powershell
uv run kamyi review path\to\book.epub --parallel --config config.yaml
```

供应商不适合并发时可改用 `--sequential`。已经完成的审校代次必须显式添加
`--force` 才会重新打开。

## 查看进度、监控和导出

```powershell
# 只读取落盘状态，不调用模型。
uv run kamyi status path\to\book.epub --config config.yaml

# 启动只读监控页面，查看阶段、Formal、Shadow 和审校事件。
uv run kamyi monitor path\to\book.epub --config config.yaml --port 8765

# 只导出已经原子提升的 Formal 正式译文。
uv run kamyi assemble path\to\book.epub --config config.yaml --format epub
```

默认情况下，可恢复状态位于 `state/`，组装结果位于 `outputs/`；这两个目录都被
Git 忽略。`assemble` 永远不会把未通过质量门的 Shadow 候选内容导出。

## 独立阶段与术语管理

只要前置状态已经存在，各个阶段可以独立运行：

```powershell
uv run kamyi stage translate path\to\book.epub --chapters 0-3 --config config.yaml
uv run kamyi stage factual-audit path\to\book.epub --chapters 0-3 --config config.yaml
uv run kamyi stage chinese-audit path\to\book.epub --chapters 0-3 --config config.yaml
uv run kamyi stage promote path\to\book.epub --chapters 0-3 --config config.yaml
```

术语规则可以通过命令管理，无需手工修改生成状态：

```powershell
uv run kamyi terms list --config config.yaml
uv run kamyi terms group-add flame 炎 火焰 --config config.yaml
uv run kamyi terms add 炎魔法 火焰魔法 --group flame --mode hard --config config.yaml
uv run kamyi terms set-status 炎魔法 active --config config.yaml
```

## 开发检查

```powershell
uv run ruff check .
uv run pytest
```

更多设计细节见 [架构说明](docs/architecture.md)、
[阶段与双通道调度](docs/design/stage-execution.md)、
[模型配置](docs/design/model-configuration.md)、
[输入输出格式](docs/formats.md)和[供应商配置](docs/providers.md)。

## 来源与许可证

文档解析与组装、可恢复存储和部分供应商适配基础复用了 Wenyi 的 MIT 许可代码。
Kamyi 在 Wenyi 的启发下重新设计了章节优先的翻译策略、提示词、修复规划、纯中文
审校边界和命令行编排。许可证见 [LICENSE](LICENSE)。
