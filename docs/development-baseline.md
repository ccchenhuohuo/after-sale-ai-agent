# Development Baseline

本项目以 GitHub 上的已提交代码作为唯一长期代码基准。服务器可以作为主要开发机器，但开发工作必须发生在 Git 工作区中，生产部署目录只保存已验证版本。

## 目录职责

```text
GitHub
  -> 唯一长期代码真相

服务器 /opt/agent-runtime-dev
  -> Agent Runtime dev，主要开发工作区
  -> 必须是 Git checkout
  -> 用于编码、测试、commit、push

服务器 /opt/agent-runtime
  -> Agent Runtime，生产部署目录
  -> 不作为日常开发目录
  -> 只部署已提交、已测试的 Git revision

本地工作区
  -> 辅助调试环境
  -> 只做轻量功能验证、文档查看和临时排查
  -> 不作为默认开发基准
```

## 基准规则

- GitHub commit 是项目唯一可追溯基准；服务器和本地都不能长期保留未提交代码。
- `/opt/agent-runtime-dev` 是服务器侧开发工作区。后续主要开发、测试、提交和 push 优先在这里完成。
- `/opt/agent-runtime` 是生产部署目录。除紧急止血外，不直接在该目录编辑业务代码。
- 本地工作区只用于简单调试、快速阅读和小范围验证。任何需要保留的本地改动都必须进入 Git 分支并 push。
- 生产目录的 `.deploy-revision` 必须记录当前部署来源，例如完整 commit hash，或明确的 commit hash 加部署标记。
- 真实私有数据、索引产物、`.env`、`.venv`、缓存、备份和 egg-info 产物不进入 Git 基准。

## 标准开发流程

1. 在服务器开发工作区同步 GitHub：

   ```bash
   cd /opt/agent-runtime-dev
   git fetch origin
   git status --short --branch
   ```

2. 在开发分支完成代码修改。分支默认使用 `codex/` 前缀。

3. 提交前运行本地快速验收：

   ```bash
   make smoke
   python3 -m pytest -q
   ```

4. 确认只包含本任务相关改动：

   ```bash
   git status --short
   git diff --stat
   ```

5. commit 并 push 到 GitHub：

   ```bash
   git add <changed-files>
   git commit -m "<type>: <summary>"
   git push origin <branch>
   ```

6. 生产部署只从已提交 revision 更新 `/opt/agent-runtime`，然后记录 revision：

   ```bash
   cd /opt/agent-runtime
   printf "%s\n" "<commit-hash>" > .deploy-revision
   ```

7. 部署后在服务器生产目录重新验收：

   ```bash
   cd /opt/agent-runtime
   make smoke
   .venv/bin/python -m pytest -q
   systemctl status agent-runtime-feishu-long.service --no-pager
   ```

## 紧急修复规则

如果必须在 `/opt/agent-runtime` 做紧急生产修复，只允许用于恢复服务。修复完成后必须立即：

- 将同一改动同步回 `/opt/agent-runtime-dev`。
- 运行 `make smoke` 和相关 pytest。
- commit 并 push 到 GitHub。
- 更新 `.deploy-revision` 到新的 Git revision。

生产目录不能成为隐藏分支，也不能长期保留只存在于服务器的业务逻辑。

## 本地调试边界

本地可以继续用于：

- 阅读代码和文档。
- 运行小范围单元测试。
- 调试纯 Python 函数或离线 smoke。
- 临时验证文档、fixture、prompt 或 schema。

本地不应作为默认位置完成大块开发、生产修复或最终验收。若本地先产生了有价值的改动，应尽快推入 GitHub，并在 `/opt/agent-runtime-dev` 拉取后继续。

