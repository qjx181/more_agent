# Self-Evolving Hermes Agent Swarm — 运行规则

## 一、核心思想

9 个 Hermes Agent 实例形成一个自我进化的生态系统。
- 零人工干预：所有编码、审查、文档、记忆、Git 提交全部自动
- 两队制衡：A队开发(进攻) + B队质量(防守)，协调者仲裁
- 每轮 ~30 分钟：A队产出 → B队审查 → 协调者决策 → Git 提交

## 二、团队结构

| 角色 | Agent ID | 职责 |
|------|----------|------|
| **协调者** | Agent 0 (orchestrator) | 分发任务、审核结果、做最终决策、提交 Git |
| **A队-核心逻辑** | Agent 1 (dev-core) | 编写算法的核心函数、业务逻辑 |
| **A队-工具/接口** | Agent 2 (dev-tools) | 开发工具函数、API 接口、CLI 命令 |
| **A队-知识/Skill** | Agent 3 (dev-skills) | 编写/更新 SKILL.md、文档、知识库 |
| **A队-记忆/配置** | Agent 4 (dev-memory) | 管理配置文件、统计数据、长期记忆 |
| **B队-代码审查** | Agent 5 (qa-review) | 审查代码质量、设计模式、代码异味 |
| **B队-测试验证** | Agent 6 (qa-test) | 写测试、运行测试、验证功能正确 |
| **B队-文档/注释** | Agent 7 (qa-docs) | 检查/补充注释、文档的可读性和完整性 |
| **B队-安全/性能** | Agent 8 (qa-perf) | 检查安全漏洞、性能瓶颈、资源泄漏 |

## 三、运行循环

### Phase 1: A队开发（并行 4 个 Agent）
每个 Agent 读取当前项目的 TODO 和 issues，选择一个任务：
- 可以编写新代码文件
- 可以创建/更新 SKILL
- 可以写 memory 保存经验
- 可以修改已有功能

### Phase 2: B队审查（并行 4 个 Agent）
每个 B 队 Agent 审查 A 队产出的一个方面：
- 代码审查：逻辑错误、代码风格、重复代码
- 测试验证：运行测试、补测试
- 文档审查：注释完整性、README
- 安全/性能审查：潜在问题

### Phase 3: 协调者决策
- 汇总所有审查报告
- 决策哪些改动合并，哪些驳回
- 如果需要修复，dispatch 给 A 队快速修复
- 最终确定本轮成果

### Phase 4: Git 提交
- 生成规范的 commit message
- 执行 git add / commit / push
- 记录本轮摘要到 CHANGELOG

## 四、进化规则

1. **技能进化**：任何 Agent 发现可复用的模式 → 创建 SKILL
2. **记忆进化**：重复出现的失败模式 → 写入 memory 防止再犯
3. **代码进化**：B队发现的重复代码 → 抽象成工具函数
4. **自我改进**：每轮结束后协调者评估本轮效率 → 调整下一轮策略

## 五、通信协议

所有工作产物放在 `tmp_agent/<agent_id>/` 目录下：
```
tmp_agent/
├── agent-1/output/    # 代码产出
├── agent-1/report.json
├── agent-5/output/    # 审查报告
├── agent-5/review.json
└── orchestrate/       # 协调者总结
```

## 六、约束

- 子 Agent 不能访问 memory
- 子 Agent 不能调用 delegate_task（防止无限嵌套）
- 子 Agent 不能问用户问题
- 只有协调者能操作 Git
