# TokenStatMenu Homebrew Tap

这个仓库为 [TokenStatMenu](https://github.com/Tupig/TokenStatMenu) 提供 Homebrew 源码安装和更新。TokenStatMenu 是原生 Swift 菜单栏应用，用于在本机查看 Seasun Token Hub 企业额度及有来源说明的 Codex Token 记录；正式 App 不依赖 Electron。

Formula 下载主项目正式 Release 中的源码包，在你的 Mac 上用 Xcode 构建当前架构的 App。安装和升级不会自动启动、关闭或重启应用，也不会替换手工保留的 App 或个人偏好。

## 安装前准备

| 项目 | 要求 |
| --- | --- |
| 操作系统 | App 最低部署目标 macOS 13；还需满足当前 Homebrew 的主机支持范围 |
| 处理器 | Apple Silicon 或 Intel，源码构建采用本机架构 |
| 构建工具 | 已完成首次启动准备的完整 Xcode 15 或更新版本；只有 Command Line Tools 不够 |
| Homebrew | 正常安装并可运行的 Homebrew |
| 使用前提 | 本机 Seasun Token Hub 已正常运行并登录 |

Formula 只在自己的构建进程内选择 Xcode，不更改全局 `xcode-select`。构建走项目原生 Xcode 工程，不通过 SwiftPM 下载测试依赖。应用运行时不需要 Python 或额外的 UI 框架。

## 安装与启动

```bash
brew install Tupig/tokenstatmenu/tokenstatmenu
tokenstatmenu
```

完整名称明确选择本仓库的 Formula，并只信任这个 Formula。`tokenstatmenu` 启动器不接受参数，使用系统 `/usr/bin/open` 打开稳定的 Homebrew `opt` 路径，不复制 App 到“应用程序”目录、不创建服务或登录项。维护工作流在临时 runner 上先 `brew trust --formula Tupig/tokenstatmenu/tokenstatmenu` 再创建本地 tap，以便 style/audit 能加载该特定配方；不会信任整个 tap 或关闭信任检查。依据见 [Homebrew 单项信任说明](https://docs.brew.sh/Tap-Trust)。

启动后点击右上角菜单栏统计文字打开面板。没有主窗口是预期行为。正常使用不需要填写 Endpoint、API Key 或配置文件；可选的界面兼容读取才涉及辅助功能权限。详细使用方法与数字定义见主项目的 [安装与使用](https://github.com/Tupig/TokenStatMenu/blob/main/docs/wiki/安装与使用.md) 和 [统计口径](https://github.com/Tupig/TokenStatMenu/blob/main/docs/wiki/统计口径.md)。

## 升级

先从 TokenStatMenu 的面板退出当前实例，再执行：

```bash
brew update
brew upgrade Tupig/tokenstatmenu/tokenstatmenu
tokenstatmenu
```

Homebrew 升级磁盘上的 App，不替换已经运行的进程；旧实例未退出时，系统 `open` 可能只激活旧实例。先退出也可以避免旧 keg 清理后进程仍在使用旧路径。Formula 不会按应用名终止任何进程。

主项目发布新的稳定版本后，本仓库约每小时检查一次；GitHub 调度可能延迟，不保证整点可用。只有完成 Release 身份检查及 macOS 实际源码构建和包装测试后，更新工作流才改写 Formula。应用自身没有后台自动更新器，仍需运行上面的 Homebrew 命令。

## 安装位置与检查

```bash
brew --prefix Tupig/tokenstatmenu/tokenstatmenu
brew list Tupig/tokenstatmenu/tokenstatmenu
brew test Tupig/tokenstatmenu/tokenstatmenu
```

安装内容为 `libexec/TokenStatMenu.app` 和 `bin/tokenstatmenu`。`brew test` 检查版本、构建号、主机架构、图标、四个 Bundle 文件、来源提交、严格签名和启动器字节；启动器只有在完整匹配后才执行拒绝参数的测试。测试不会打开 App、访问企业服务或扫描个人会话，因此通过不等于真实企业数据已经接通。

构建产物使用本地 **ad hoc 签名**，不是 Developer ID 签名或 Apple 公证。`codesign --verify` 检查通过只能说明签名结构正确；如果系统或企业策略阻止启动，应按策略核实来源，Formula 不会删除隔离属性、绕过 Gatekeeper 或更改系统权限。

Info.plist 中 `TokenStatSourceRevision` 记录经过标签校验的公开源码提交，`TokenStatSourceState` 固定为 `release-source`。源码 ZIP 没有 App 的 Git 工作区，因此不会把它标成 `clean`，也不会继承外层 tap 的提交身份。

## 卸载与回退

先在应用面板退出 TokenStatMenu，再运行：

```bash
brew uninstall Tupig/tokenstatmenu/tokenstatmenu
brew untap Tupig/tokenstatmenu
```

卸载仅移除 Homebrew 管理的 Formula，不删除个人偏好、手工 App 或 Hub/Codex 数据。若保留了先前手工安装的 App，可在退出 Homebrew 版本后打开那一份；切换前确认安装路径，避免同一 Bundle ID 的多个实例造成混淆。不要把 Homebrew 缓存视为长期保留旧版本的保证。

## 维护者：来源校验与自动更新

公开文件限于许可证、本文、Formula、一个 Python 标准库更新器、一份测试和两个工作流。

| 文件 | 责任 |
| --- | --- |
| [Formula/tokenstatmenu.rb](Formula/tokenstatmenu.rb) | 固定源码配方、无参数启动器、无 GUI 启动的包装测试 |
| [script/update_formula.py](script/update_formula.py) | 固定公共 Release 的有界读取、校验、配方生成和发布前检查 |
| [Tests/test_update_formula.py](Tests/test_update_formula.py) | 虚构 Release、ZIP、HTTP、版本、无写入及工作流权限边界 |
| [.github/workflows/verify.yml](.github/workflows/verify.yml) | push/PR 的只读检查及 macOS 原生构建测试 |
| [.github/workflows/update.yml](.github/workflows/update.yml) | 定时/手动检查、同一候选的 macOS 测试、单文件发布 |

更新源固定为公开 `Tupig/TokenStatMenu`。只接受非草稿、非预发布的数字稳定版本 `vMAJOR.MINOR.PATCH`，按数字比较，不降级；同版本任一 pin 字段冲突会失败关闭。标签必须解析为实际提交，不能用 Release 的分支名代替。

当前 1.1.4 历史 Release 正好包含两个 ZIP、`release-manifest.json` 和 `SHA256SUMS`；从 1.1.5 起，稳定 Release 还必须包含同一 App 的 Universal DMG，合计五项资产。更新器交叉核对版本、构建、标签提交、文件名、字节数、摘要和完整资产集合；它对源码 ZIP 做路径、类型、CRC、数量/体积上限及原生工程检查，DMG 的镜像内容由主项目 macOS 发布校验器负责。它不会执行下载的 Ruby 或源码来决定是否更新，也不会接受自定义仓库或任意下载地址。

自动更新采用三个 job：

1. `check` 在 Ubuntu 上以只读权限检查 Release，输出固定结构的候选 pin 和旧 Formula Git blob SHA。无新版本或无 Release 时不写入。
2. `smoke` 在临时 macOS runner 重建完全相同的候选，依次运行单元检查、`brew style`、`brew audit`、实际源码安装和 `brew test`。
3. `publish` 在 Ubuntu 上重新检查最新 Release 的全部 pin 字段及旧 blob SHA，只用本仓库 `GITHUB_TOKEN` 对 `Formula/tokenstatmenu.rb` 执行一次 Contents API 更新。期间出现新版本、资产变化或并发修改会中止，不强行覆盖。

公开 Release 下载不需要 PAT、企业凭据或跨仓库 token。写入 job 不构建/执行 App 源码；PR 不进入写入流程。`GITHUB_TOKEN` 产生的提交通常不会触发普通 push CI，所以 macOS 测试明确在写入之前执行，不能依赖事后的递归 CI。

本地合成检查：

```bash
python3 -B -m unittest discover -s Tests -v
```

默认跳过的是 PM 控制的真实已安装隔离 Formula 校验，不代表真实安装已通过。正式安装、两版本升级和 GitHub 工作流的端到端结果必须分别记录；合成 fixture 不能代替实际 Release 下载。

## 问题反馈与许可证

应用数据或界面问题请在 [主项目 Issues](https://github.com/Tupig/TokenStatMenu/issues) 反馈；Formula 安装或更新问题可在 [本仓库 Issues](https://github.com/Tupig/homebrew-tokenstatmenu/issues) 反馈。提供 macOS/Xcode/Homebrew 版本、失败步骤及脱敏错误类别即可，不附 API Key、企业地址、账号、真实额度、会话正文或原始运行日志。

本仓库采用 [MIT 许可证](LICENSE)。
