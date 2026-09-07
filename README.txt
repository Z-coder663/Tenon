CAworker 本地编程智能体

项目概况
CAworker 是一个面向本地代码仓库的多轮编程 Agent。项目不依赖 LangChain 等 Agent
框架，直接实现模型协议、ReAct 工具循环、上下文管理、会话持久化和权限控制。用户可
通过 TUI 或 Web UI 输入自然语言任务，由 Agent 阅读项目、制定计划、修改代码、运行
命令并验证结果。

一、Git 仓库地址
https://github.com/STL250/agent

二、如何运行
运行环境：Python 3.10 及以上，无运行时第三方依赖。
1. 在源码目录安装：python -m pip install -e .
2. 将 .env.example 复制为 .env，填写 RIVET_BASE_URL、RIVET_MODEL 和
   RIVET_API_KEY。真实 API Key 不得提交到 Git。
3. 检查模型兼容性：rivet --check-model
4. 在目标项目根目录启动：
   TUI：rivet
   Web UI：rivet --web

三、特色功能说明
1. 主 Agent 负责理解任务、调用工具、修改文件和验证结果；只读的 Explorer 与
   Reviewer 子 Agent 分别负责代码探索和风险审查。
2. 会话按项目持久化，支持多轮交互、恢复、失败重试和按轮次安全撤销。
3. 长对话采用固定结构摘要和普通关键词检索进行压缩，完整保留工具调用与结果的对应
   关系，不使用 RAG 或向量数据库。
4. 文件修改后旧验证自动失效，只有在最新修改之后成功运行测试、编译等命令，任务才
   能标记为已验证完成。
5. Skills 分为内置、用户级和项目级三层并按需加载；safe、ask、never 三种模式分别
   对应安全操作自动执行、修改逐项确认和只读分析。
6. TUI 与 Web UI 可展示流式回复、任务计划、项目文件、命令结果、子 Agent 状态和
   行级代码改动。

四、其他说明
文件访问限制在当前工作区；命令具有超时、输出限制和危险操作拦截；API Key 不写入
会话或前端。详细架构和模型配置见 docs 目录。
