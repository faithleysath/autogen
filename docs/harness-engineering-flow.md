# Harness Engineering Flow

```mermaid
flowchart LR
    U([用户输入: PRD + Repo]) --> PRD[[PRD]]

    PRD --> A["架构师 agent<br/>读取当前代码和输入内容"]
    A --> SPEC[[execution-contract.md<br/>冻结验收口径]]
    A --> PLAN[[architecture-plan.md<br/>支持单阶段或多阶段]]
    A --> E2EP[[e2e-plan.md]]
    PLAN --> D[开发 agent]

    SPEC -. 规范基线 .-> C["规范符合度审查<br/>静态代码审查与执行合同对比"]
    SPEC -. 验收基线 .-> E[E2E 验收]

    D --> G{"阶段门禁<br/>1. 本阶段不过关，继续当前阶段<br/>2. 本阶段过关，进入下一阶段<br/>3. 所有阶段完成，放行"}
    G -->|不过关| D
    G -->|过关，进入下一阶段| D
    G -->|所有阶段完成| C
    G -->|所有阶段完成| Q["工程 QA<br/>静态检查 / lint / typecheck / 单测 / 失败归因"]
    G -->|所有阶段完成| E[E2E 验收]

    E2EP --> E

    C --> PUB["publisher workspace / orchestrator<br/>串行发布 review 报告"]
    Q --> PUB
    E --> PUB
    PUB --> R{"发布门禁<br/>1. 需要返工<br/>2. 通过"}

    R -->|通过| O([交付])
    R -->|需要返工| REWORK[[返工建议]]
    REWORK --> A

    classDef artifact fill:#f6f8fa,stroke:#4b5563,color:#111827,stroke-width:1px;
    classDef gate fill:#fff7ed,stroke:#c2410c,color:#7c2d12,stroke-width:1.5px;
    classDef terminal fill:#eff6ff,stroke:#2563eb,color:#1e3a8a,stroke-width:1px;

    class PRD,SPEC,PLAN,E2EP,REWORK artifact;
    class G,R gate;
    class U terminal;
```

这张图表达的是一个完全自治的 harness engineering 闭环：

- 用户只在起点输入 `PRD + 仓库地址`，之后从规划到交付都不需要人类介入。
- `PRD` 是原始输入，不直接充当后续所有 agent 的统一判定基线。
- `架构师 agent` 先把 `PRD` 收敛成一份冻结的 `execution-contract.md`，再输出 `architecture-plan.md` 与 `e2e-plan.md`。
- 从规划完成开始，`execution-contract.md` 成为 `开发 agent`、`规范符合度审查`、`E2E 验收` 共享的唯一需求口径；后续 agent 不再各自自由解释 `PRD`。
- `开发 agent` 只执行当前阶段；阶段不通过时继续当前阶段，通过后进入下一阶段，直到全部完成。
- 三路验证都针对同一个冻结的 `candidate_code_sha`；它们各自生成报告，但报告进入 `run_branch` 的动作由 publisher workspace / `orchestrator` 串行收口。
- 任一发布门禁不通过时，返工建议不会直接交给开发，而是回流给 `架构师 agent` 重新规划。

## 概念定义

- `execution-contract.md` 是架构阶段冻结的结构化执行规格，用来把 `PRD` 里的产品语言、模糊表述和开放问题，收敛成后续可实现、可审查、可验收的明确条目。
- `execution-contract.md` 至少应明确：交付范围、非目标、页面 / 路由 / API / 数据持久化等显式交付项、关键交互、边界条件、验收标准，以及因 `PRD` 歧义而采用的显式假设。
- 如果 `PRD` 存在模糊点，必须先在 `execution-contract.md` 中转化为明确假设或约束；后续 agent 只能按合同执行，不能各自二次解释。
- `规范符合度审查` 指静态代码审查与 `execution-contract.md` 对比，关注“是否做了、是否做全了、是否和冻结后的需求口径一致”。
- 典型检查包括：`execution-contract.md` 要求有 5 个页面，但代码里只发现 4 个路由；`execution-contract.md` 要求拖拽交互，但代码里没有对应组件或交互实现；`execution-contract.md` 要求本地持久化，但代码里未发现存储实现。
- `e2e-plan.md` 负责把 `execution-contract.md` 中的关键验收条目映射为端到端场景，避免每次验收都重新解释原始 `PRD`。
- `工程 QA` 负责工程质量与测试闭环，包含静态检查、`lint`、`typecheck`、单元测试、测试补全、失败归因。
- `工程 QA` 的关注点不是“需求是否齐全”，而是“实现是否稳定、可验证、失败原因是否可定位”。
- publisher workspace / `orchestrator` 负责把三路验证报告按固定顺序串行发布到 `run_branch`，从而保证 review 工作区始终围绕同一个候选代码快照执行。
