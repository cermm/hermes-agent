---
sidebar_position: 8
title: 身份验证存储权限
---

# 身份验证存储权限

Hermes 通过一个已配置的权限位置处理所有 OAuth 和凭据池的读取、写入、刷新、状态检查及锁定。默认使用 `~/.hermes/auth.json` 这一共享存储，因此命名配置文件无需复制令牌即可使用同一次登录。

## 配置

在当前配置文件的 `config.yaml` 中设置权限：

```yaml
auth:
  authority: shared  # shared | profile
```

- `shared`（默认）：`~/.hermes/auth.json`。命名配置文件使用默认 Hermes 根目录。
- `profile`：`<profile-home>/auth.json`。仅在需要有意隔离凭据时使用。

无效的权限值会以关闭方式失败。Hermes 不会静默切换到其他凭据存储。

### 现有配置文件

如果缺少 `auth.authority`，且现有命名配置文件已经包含 `auth.json`，Hermes 会在有限的旧版兼容模式中暂时选择该配置文件本地存储。没有本地存储的新配置文件使用 `shared`。请显式配置权限，或使用下述迁移流程消除歧义。

## 检查当前权限

```bash
hermes auth status
hermes doctor
```

这些命令仅显示所选模式、规范化存储及锁路径、来源、权限、旧版兼容状态、非权威冲突存储和最新迁移阶段；不会列出令牌或完整凭据内容。

## 将配置文件存储迁移到共享权限

迁移必须先进行 dry-run，并显式指定配置文件范围：

```bash
hermes auth migrate-shared --profile coder --dry-run
hermes auth migrate-shared --all-profiles --dry-run
```

dry-run 输出脱敏清单、`plan_id` 和 `plan_digest`。完整前置条件哈希仅保存在权限为 0600 的私有计划文件中。检查提供商拓扑后，应用完全相同的计划：

```bash
hermes auth migrate-shared --profile coder --apply \
  --plan-id <id> --plan-digest <digest> --conflict-policy abort
```

冲突策略：

- `abort`：在提交不同的提供商条目前停止。
- `prefer-shared`：保留共享条目。
- `prefer-profile`：用所选配置文件的条目替换不同的共享条目。使用 `--all-profiles` 时，按稳定的配置文件名称顺序合并。

应用过程按稳定的字节路径顺序获取所有相关身份验证锁，确认源文件和配置自 dry-run 后未改变，创建私有恢复备份和日志，原子写入共享存储，然后把所选配置文件改为 `auth.authority: shared`。旧的配置文件本地 `auth.json` 会原样保留为恢复材料，但不再是权威存储。

中断后可检查状态并恢复：

```bash
hermes auth migrate-recover --plan-id <id>
```

恢复可重复执行。已提交的迁移不会被隐式回滚。显式回滚使用 `hermes auth migrate-shared --rollback --plan-id <id>`；如果共享存储或迁移后的配置已经发生变化，回滚会拒绝覆盖后续轮换或配置修改。

## 备份与恢复

普通备份不包含身份验证数据。若需包含，必须显式加密：

```bash
hermes backup --auth-mode include-encrypted --auth-passphrase-file /secure/passphrase
```

恢复必须显式指定目标：

```bash
hermes import backup.zip --auth-action restore-shared --auth-passphrase-file /secure/passphrase
hermes import backup.zip --auth-action restore-profile --auth-passphrase-file /secure/passphrase
```

Hermes 会在提取普通文件前验证加密信封、口令、拓扑和网关静止状态。身份验证数据与当前 `config.yaml` 在规范化身份验证锁下共同提交，失败时共同回滚。所有解析到目标权限的活动网关都必须先停止。

快速快照记录解析后的拓扑，并在规范化锁下复制权威存储。普通快速恢复跳过凭据。程序化恢复必须同时传入 `include_auth=True` 和显式 `auth_action`；拓扑不匹配时，在改变非身份验证文件前失败。

## 配置文件生命周期

- `hermes profile create NAME` 创建显式 `shared` 配置文件。
- `hermes profile create NAME --auth-mode profile` 创建配置文件本地权限。克隆永不复制凭据。
- 重命名前会停止网关及 Desktop/后端写入进程，再移动配置文件本地权限状态。
- 删除配置文件本地权限时必须指定 `--auth-action archive` 或 `--auth-action purge`。归档仅在所有已知写入进程停止后执行。删除一个配置文件绝不会删除共享凭据。

## Docker 与 NixOS

Docker 引导和 Nous 会话重新引导解析同一个规范化权限，并通过同一个带锁帮助程序修改它。引导只创建缺失文件。重新引导仅替换终止状态或可证明更旧的 Nous 条目，绝不会覆盖健康且更新的会话。

在 NixOS 上，`services.hermes-agent.authAuthority` 会生成对应的 `auth.authority` 设置。`authFile` 仅用于一次性种子：激活时原子创建缺失目标，永不覆盖现有存储。即使提供 `services.hermes-agent.configFile`，模块仍会把 `authAuthority` 合并到安装后的配置中，使声明拓扑与种子目标不会分歧。系统有意不提供强制覆盖开关。

## 第一方消费者清单

所有第一方身份验证存储消费者都必须通过 `hermes_cli.auth_authority`（或独立 Docker/Nix 等效帮助程序）解析路径；写入时必须使用与解析后数据路径配对的锁。

| 消费者 | 模块 |
| --- | --- |
| CLI 登录/登出/状态和提供商设置 | `hermes_cli.auth` |
| 设置向导/提供商就绪检查 | `hermes_cli.main` |
| 动态模型缓存失效 | `hermes_cli.models` |
| 凭据池刷新/账户轮换 | `agent.credential_pool` |
| 辅助模型 | `agent.auxiliary_client` |
| 网关启动迁移门 | `gateway.run` |
| 诊断 | `hermes_cli.auth_commands`, `hermes_cli.doctor` |
| 备份和配置文件生命周期 | `hermes_cli.backup`, `hermes_cli.profiles` |
| 托管工具子进程 | `tools.managed_tool_gateway` |
| xAI OAuth | `tools.xai_http` |
| Photon OAuth | `plugins.platforms.photon.auth` |
| Docker 引导/重新引导 | `scripts/docker_auth_authority.py`, `scripts/docker_rebootstrap_nous_session.py` |
| NixOS 激活种子 | `scripts/nix_auth_authority.py` |

## 应用前的安全回滚

应用前不会修改任何实时文件。可删除不需要的私有 dry-run 计划文件，或创建新计划。应用中断后请使用 `migrate-recover`；Hermes 进程运行时不要手动复制令牌文件。
