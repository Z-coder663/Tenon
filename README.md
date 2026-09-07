# CAworker

> 一个从零实现、面向本地代码仓库的多轮编程 Agent。

[![CI/CD](https://github.com/Z-coder663/Tenon/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/Z-coder663/Tenon/actions/workflows/ci-cd.yml)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-2f2f2f)

CAworker 不依赖 LangChain 等 Agent 框架，直接实现模型协议、Agent Loop、上下文管理、会话持久化、权限控制和本地工具执行。它通过终端界面理解代码、制定计划、修改文件、运行验证，并在多轮会话中持续协作。

项目展示名称为 **CAworker**；为保持已有安装方式兼容，Python 包和 CLI 命令仍使用 `rivet`。

## 核心能力

| 能力 | 当前实现 |
| --- | --- |
| Agent Loop | OpenAI-compatible Chat Completions、SSE 流式响应、Function Calling 和工具结果回传 |
| 工程工具 | 文件列表、读取、搜索、原子写入、精确替换、Diff 和本地命令执行 |
| 完成证据 | 文件修改使旧验证失效，最新修改后必须运行成功的验证命令 |
| 多轮会话 | 按项目保存、列出、恢复和继续会话，保留计划、Diff 与验证证据 |
| Context 管理 | 结构化压缩、近期原文保留、归档原文和关键词检索 |
| Plan | 显式维护 pending、in_progress、completed、blocked 状态 |
| Skills | 内置、用户级、项目级三层 Skill，按需激活 |
| 权限 | safe、ask、never 三种模式，配合路径边界与危险命令拦截 |
| 模型边界 | Agent 依赖统一 ModelClient，当前支持 OpenAI-compatible 协议 |
| 终端交互 | TUI 展示流式回复、计划、工具活动、Diff、Session 和 Skills |

当前版本采用单个主 Agent。它直接负责代码探索、实现、验证和审查。

## 快速开始

环境要求：Python 3.10 或更高版本，以及支持 Chat Completions 和 Function Calling 的模型服务。

```bash
git clone https://github.com/Z-coder663/Tenon.git
cd Tenon
python -m pip install -e .
```

复制配置模板：

```powershell
Copy-Item .env.example .env
```

填写本地 `.env`：

```dotenv
RIVET_PROTOCOL=openai_chat
RIVET_BASE_URL=https://provider.example/v1
RIVET_ENDPOINT_PATH=/chat/completions
RIVET_MODEL=your-model-name
RIVET_API_KEY=your-api-key
RIVET_AUTH_STYLE=bearer
```

首次配置后检查模型能力：

```bash
rivet --check-model
```

在需要操作的项目目录启动 TUI：

```bash
rivet
```

也可以显式指定工作区：

```bash
rivet --workspace path/to/project
```

## TUI 命令

| 命令 | 功能 |
| --- | --- |
| `/help` | 查看命令说明 |
| `/status` | 查看会话、计划、上下文和验证状态 |
| `/plan` | 查看当前计划 |
| `/diff [path]` | 查看本次会话产生的文件改动 |
| `/skills` | 查看可用及最近使用的 Skills |
| `/permissions [safe\|ask\|never]` | 查看或切换权限模式 |
| `/sessions` | 列出最近保存的会话 |
| `/resume [id]` | 恢复最近或指定会话 |
| `/new` | 新建干净会话 |
| `/exit` | 保存并退出 |

任务执行期间按 `Ctrl+C` 可取消当前模型请求、审批等待或命令，程序会返回当前会话输入。

## 工具

- `update_plan`
- `list_files`
- `read_file`
- `search_text`
- `search_history`
- `write_file`
- `replace_text`
- `show_diff`
- `run_command`
- `list_skills`
- `activate_skill`
- `read_skill_resource`

工具参数在执行前经过本地 Schema 校验。文件操作限制在当前工作区；命令具有超时、输出限制、危险操作拦截和取消支持。

## 项目结构

```text
Tenon/
├─ .github/workflows/ci-cd.yml
├─ docs/
│  ├─ ARCHITECTURE.md
│  ├─ CAWORKER_GAP_ANALYSIS.md
│  └─ PROVIDERS.md
├─ src/rivet/
│  ├─ agent.py
│  ├─ client.py / provider.py
│  ├─ context.py
│  ├─ session.py
│  ├─ skills.py
│  ├─ tools.py
│  ├─ workspace.py
│  ├─ plan.py
│  ├─ tui.py
│  └─ cli.py
├─ .env.example
└─ pyproject.toml
```

更完整的模块边界见 [架构文档](docs/ARCHITECTURE.md)，模型服务配置见 [Provider 文档](docs/PROVIDERS.md)，当前缺口与开发顺序见 [Gap Analysis](docs/CAWORKER_GAP_ANALYSIS.md)。

## License

本项目使用 [MIT License](LICENSE)。
