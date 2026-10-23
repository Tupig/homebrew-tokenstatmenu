# TokenStatMenu Homebrew Tap

**简体中文** | [English](#homebrew-tap-english)

[TokenStatMenu](https://github.com/Tupig/TokenStatMenu) 是 Seasun Token Hub 的原生 macOS 菜单栏额度工具。这个 Tap 从正式 Release 源码包构建 App，提供 Homebrew 安装与升级。

## 安装

1.1.7 起只支持 **Apple Silicon（M 系列）**。需要 macOS 13+、Homebrew 和完成首次启动准备的完整 Xcode 15+，仅 Command Line Tools 不够。

```bash
brew install Tupig/tokenstatmenu/tokenstatmenu
brew test Tupig/tokenstatmenu/tokenstatmenu
tokenstatmenu
```

先打开并登录 Seasun Token Hub。App 不需要填写 API Key，也不修改 Hub 数据库。构建只在自身进程选择 Xcode，不更改全局 `xcode-select`。

安装内容为 Homebrew `libexec/TokenStatMenu.app` 和无参数启动器 `tokenstatmenu`。不会复制到个人应用目录、自动启动、杀进程或更改偏好。

## 更新

先从 App 面板退出旧实例：

```bash
brew update
brew upgrade Tupig/tokenstatmenu/tokenstatmenu
brew test Tupig/tokenstatmenu/tokenstatmenu
tokenstatmenu
```

升级替换磁盘文件，不替换运行中的进程。不要同时启动手动副本。个人偏好与手动 App 都会保留。

Tap 每小时检查稳定 Release，也可由维护者手动触发。检查身份、摘要和源码后，在 macOS 构建并执行 style/audit/test，最后单独写入 Formula。配方更新后，用户仍需运行 `brew update` 和 `brew upgrade`。

## 签名与小组件

本机采用 ad hoc 签名，不是 Developer ID 或 Apple 公证。1.1.7 配方包含 Widget 扩展，先签扩展、再签主 App，检查八文件结构和 arm64。但 ad hoc 签名不保证 App Group 授权；系统小组件可能无法获取数据，菜单栏和面板仍可用。见 [#88](https://github.com/Tupig/TokenStatMenu/issues/88)。

`brew test` 不启动 GUI，不访问企业服务，也不能代替真实小组件验收。配方不会清除隔离属性、重置权限或绕过 Gatekeeper。

## 下载与开发

不想编译可用主项目的 [DMG](https://github.com/Tupig/TokenStatMenu/releases)。1.1.7 资产名为 `TokenStatMenu.dmg`、`TokenStatMenu.zip` 和 `TokenStatMenu-source.zip`，版本在 Release 标签中。历史版本保留旧命名和旧模板。

```bash
python3 -B -m unittest discover -s Tests -v
```

更新器验证固定仓库、稳定标签、提交、manifest、SUMS 和源码成员，拒绝降级、同版冲突及并发配方变更。只有测试后的独立发布 job 有写权限。

[使用文档](https://github.com/Tupig/TokenStatMenu/blob/main/docs/wiki/Home.md) · [问题反馈](https://github.com/Tupig/TokenStatMenu/issues) · [MIT](LICENSE)

---

# Homebrew Tap (English)

[简体中文](#tokenstatmenu-homebrew-tap) | **English**

This Tap builds [TokenStatMenu](https://github.com/Tupig/TokenStatMenu), a native macOS menu bar app for Seasun Token Hub quotas, from verified Release source archives.

## Install

Version 1.1.7 onward requires **Apple Silicon (M-series)**, macOS 13+, Homebrew and full Xcode 15+ with first-launch setup completed. Command Line Tools alone are insufficient.

```bash
brew install Tupig/tokenstatmenu/tokenstatmenu
brew test Tupig/tokenstatmenu/tokenstatmenu
tokenstatmenu
```

Open and sign in to Hub first. No API Key entry is needed; the Hub database is not modified. Xcode selection is process-local, not a global `xcode-select` change.

The App lives under Homebrew `libexec`; `tokenstatmenu` is a no-argument launcher. Installation does not copy into personal Applications, launch the GUI, kill processes or change preferences.

## Upgrade

Quit the old instance from its panel:

```bash
brew update
brew upgrade Tupig/tokenstatmenu/tokenstatmenu
brew test Tupig/tokenstatmenu/tokenstatmenu
tokenstatmenu
```

Upgrades replace files, not running processes. Do not run manual and Homebrew copies together. Preferences and manual Apps are preserved.

The Tap checks stable Releases hourly or on manual dispatch. Identity, digest and source validation precede macOS build/style/audit/test. A separate publication job then updates the formula. Users still need to run update and upgrade.

## Signing and Widget

Builds are ad hoc signed, not Developer ID signed or notarized. The 1.1.7 formula embeds the Widget, signs it before the App and validates the eight-file arm64 Bundle. Ad hoc signing does not guarantee App Group authorization, so Widget data may be unavailable; the menu bar and panel remain usable. See [#88](https://github.com/Tupig/TokenStatMenu/issues/88).

`brew test` does not launch the GUI or access enterprise services. It is not a real Widget acceptance test. The formula does not remove quarantine, reset permissions or bypass Gatekeeper.

## Downloads and Development

Use the main project's [DMG](https://github.com/Tupig/TokenStatMenu/releases) for a prebuilt App. Version 1.1.7 uses `TokenStatMenu.dmg`, `TokenStatMenu.zip` and `TokenStatMenu-source.zip`; the tag carries the version. Historical naming and templates remain unchanged.

```bash
python3 -B -m unittest discover -s Tests -v
```

The updater validates the repository, stable tag, commit, manifest, SUMS and source members. Downgrades, same-version conflicts and concurrent formula changes are rejected. Only a separate post-test publication job can write.

[Documentation](https://github.com/Tupig/TokenStatMenu/blob/main/docs/wiki/Home.md) · [Issues](https://github.com/Tupig/TokenStatMenu/issues) · [MIT](LICENSE)
