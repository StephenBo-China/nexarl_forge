# vibe_coding_manage_platform Memory Proposals

## Pending Project Long-Memory Candidates

### 2026-07-14 - 多对话 Worktree、Release 队列与 canonical 同步机制

- 中央管理平台新增 `scripts/worktree_flow.py` 和 `docs/worktree_loop_workflow.md`，统一管理跨项目多对话 Worktree/Loop 生命周期。
- 原始仓库定义为 canonical workspace；任务开发使用仓库外部的一对话一 worktree 一分支映射。
- 功能开发允许并行；主分支整合、主分支推送、canonical 同步和共享 staging 部署通过仓库级 release/staging lock 串行执行。
- 主分支整合使用基于最新远端主分支的临时 release worktree，禁止 force push、自动 stash、reset 和覆盖用户文件。
- canonical 同步仅使用 ff-only；脏文件与远端更新路径重叠、当前分支错误或历史分叉时必须阻止并报告。
- 最终完成要求功能提交是远端主分支祖先，且远端主分支、canonical 主分支和部署 commit 一致。
- Loop 配置模板升级为 schema v2；`upgrade-loop` 只补充缺失安全字段，不覆盖项目专用 staging、数据库、OSS 或端口配置。
