# Sub2API Overdraft Auto Builder

面向原生 Linux 部署的 Sub2API 私有融合构建流水线。仓库定时跟踪官方版本和 HTExplicit codexrip 分支，把其功能与版本化二次元 UI overlay 合并到临时源码树；只有编译和测试全部通过后，才发布可供面板人工应用的候选包。

## 上游项目

- 官方项目：[Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api)
- 融合功能：[HTExplicit/sub2api](https://github.com/HTExplicit/sub2api/tree/main)

本仓库不是上述项目的官方发布渠道。两个上游项目均使用 LGPL-3.0；完整归属见 [NOTICE](NOTICE)，每个 Release 的 `build-metadata.json` 会记录实际使用的版本、提交和 SHA-256。

## 工作流

```text
每 4 小时 / 手动触发
        |
        v
锁定官方 Release 提交 + HTExplicit 提交 + UI 清单
        |
        v
计算输入指纹，未变化则停止
        |
        v
临时目录融合源码并应用 UI overlay
        |
        v
前端类型检查与测试 + Go 定向测试 + 迁移测试 + 原生编译
        |
        v
发布 Linux amd64 Release + SHA256SUMS + 构建证据
        |
        v
成功后才更新 state/upstreams.json
```

- [auto-build.yml](.github/workflows/auto-build.yml) 每 4 小时检测一次，也支持手动强制构建。
- [validate.yml](.github/workflows/validate.yml) 校验 Python、UI 清单、密钥泄露和单元测试。
- 官方 `v0.2.1` 与 HTExplicit 的合并基线一致时，重放锁定的 codexrip 变更。
- 当前 `v0.2.1-codexrip.2` 重放明确排除 HTExplicit 临时的
  `.github/workflows/ssh-deploy-key-probe.yml`；该工作流会读取生产环境 SSH
  密钥并连接目标主机，不属于本自用包。排除项会写入 `build-metadata.json`，检测器
  也会拒绝包含该路径的补丁。
- 0.2.1 的回放测试不依赖上游分支私有的 downstream workflow 文件；这些文件不在
  官方 Release 源码中，因此不会被纳入融合补丁。
- 官方版本领先时，从 HTExplicit 提交生成的审核补丁对新的官方提交执行三方重放。
- UI 没有精确版本时，会尝试重放最近的旧 overlay，但会先校验每个被 overlay 覆盖的上游文件 SHA-256；任何文件漂移或原本不存在的目标文件出现时都会停止发布并要求人工适配。任何校验、类型检查、测试或编译失败也都会阻止发布。
- 远程技能种子中的 Unicode 文件名只在临时构建树中转换为确定性的 ASCII 嵌入名，
  逻辑路径与内容哈希不变，避免 Go `embed` 工具链差异导致误报构建失败。
- 构建作业只有仓库只读权限；发布作业不执行上游代码。

## 产物

每个成功 Release 包含：

- `sub2api`：Linux amd64 原生二进制。
- `build-metadata.json`：官方、HTExplicit、UI overlay 和测试证据。
- `SHA256SUMS`：所有发布文件的 SHA-256。
- `fusion-*.tar.gz`：包含二进制、元数据、许可证和归属声明的部署包。

Release 标签由所有不可变输入生成：

```text
fusion-v<融合版本>-<官方提交8位>-<透支提交8位>-u<UI清单8位>
```

## 与 2222 面板的边界

Actions 负责跟踪两个上游、融合、测试和发布，不会连接生产服务器。服务器上的 `release_monitor.py` 每 3–5 小时执行一次，只做以下工作：

1. 查询官方最新 Release 和本仓库 `auto-build.yml` 状态。
2. 校验融合 Release 必须由 `github-actions[bot]` 发布。
3. 校验 Release 标签、`build-metadata.json`、测试状态和 `SHA256SUMS`。
4. 官方版本领先融合 Release 时显示“等待仓库编译”，不下载旧候选包。
5. 下载并复核候选二进制的大小与 SHA-256，随后等待人工应用。

2222 面板不拉取两个上游源码，也不运行 Go、Node 或 pnpm。只有用户点击“应用已验证版本”后，服务器才全量备份程序、配置和 PostgreSQL，然后原子切换、重启并执行健康检查；失败时恢复旧程序和数据库。构建失败只在面板显示告警和 Actions 链接，当前服务保持不变。

应用单元在真正开始备份前会以与替换操作相同的服务用户执行写入预检
（`check-binary-write`）。如果 `/opt/sub2api` 是只读挂载，或 systemd 单元没有
`ReadWritePaths=/opt/sub2api`，任务会立即失败并保留旧程序和数据库，不会留下
`.sub2api.*.new` 残留文件。此时先在服务器执行以下只读检查，再重新安装本集成：

```bash
systemctl show sub2api-overdraft-apply.service -p ProtectSystem -p ReadWritePaths -p ReadOnlyPaths
findmnt -T /opt/sub2api -o TARGET,FSTYPE,OPTIONS
sudo -u sub2api touch /opt/sub2api/.write-test
rm -f /opt/sub2api/.write-test
```

`findmnt` 若显示 `ro`，必须先恢复可写挂载；应用逻辑不会尝试偷偷 remount，也不会
把临时文件跨文件系统搬到状态目录。重新安装 2222 集成后，安装脚本会校验 apply
单元的读写白名单并在不符合时自动回滚安装文件。

## 本地校验

```bash
python3 -m compileall -q manager.py auto_update.py release_monitor.py scripts tests
python3 scripts/validate_repository.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/detect_updates.py --output build/detection.json
python3 scripts/build_candidate.py --detection build/detection.json --output dist
```

完整构建需要 Go 1.26.6、Node.js 24 和 pnpm 9 或 10，这些工具由 GitHub Actions 使用，不要求安装在生产服务器。数据库备份与原子切换工具的配置样例位于 `config/manager.env.example`。

## 更新策略

对账户、额度、请求链路、数据库迁移和 UI 的上游变化采用失败即停止策略。流水线能减少官方更新覆盖定制功能的风险，但上游发生结构性改动时仍需人工适配，不能承诺永久免维护。

## 安全

不要向仓库提交 GitHub PAT、服务器密码、数据库连接串或 SSH 私钥。Actions 使用 GitHub 自动签发的短期 `GITHUB_TOKEN`；细节见 [SECURITY.md](SECURITY.md)。
