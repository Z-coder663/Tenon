CAworker 本地编程智能体

CAworker 是一个面向本地代码仓库的多轮编程 Agent。项目不依赖 Agent 框架，直接实现
模型协议、Agent Loop、上下文管理、会话持久化、权限控制和本地工具执行。

仓库地址
https://github.com/Z-coder663/Tenon

运行方式
1. Python 3.10 或更高版本。
2. 在源码目录执行：python -m pip install -e .
3. 将 .env.example 复制为 .env，填写模型地址、模型名称和 API Key。
4. 检查模型兼容性：rivet --check-model
5. 在目标项目目录启动终端界面：rivet

当前结构
1. 单个主 Agent 负责代码探索、修改、验证和审查。
2. TUI 提供流式回复、计划、Diff、权限、Skills 和 Session 管理。
3. 会话按工作区持久化，支持多轮交互与恢复。
4. Context 支持结构化压缩、近期原文保留和归档检索。
5. 文件修改后，必须在最新修改之后运行成功的验证命令才能标记为已验证完成。
6. safe、ask、never 三种模式控制修改和命令权限。

详细架构、模型配置和当前缺口见 docs 目录。
