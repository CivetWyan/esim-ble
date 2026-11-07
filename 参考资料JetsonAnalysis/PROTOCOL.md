# 9eSIM 蓝牙管理协议与 Mac 实现记录

记录日期：2026-11-07（北京时间）。本文区分实机观察、应用字节证据、上游源码行为和待验证能力；Mac 主客户端已完成本机发布构建，并实际认证目标卡、显示 EID 和三份配置；尚不能宣称 profile 写入／启停／改名／删除已全部实测成功。

## 1. 目标与证据身份

- 安卓手机上目标包：`ee.nekoko.nlpa.flavor1`，包管理器报告版本 `2.2.4`、versionCode `586`；前台 Activity 是 `ee.nekoko.nlpa2.MainActivity`。
- 提取的 APK：[9esim-installed.apk](analysis/9esim-bluetooth/apk/9esim-installed.apk)，SHA-256 为 `1739ea8fa26d5264d61f32e37c6dae0b03914356dcd32ab25c010eefcc91da98`。
- 应用是 Flutter 发布构建，主要 Dart AOT 字节位于 APK 内 `lib/arm64-v8a/libapp.so`，已提取到同目录 `libapp.so`（12,125,064 字节）。
- 上游源码：[iebb/NekokoLPA2](https://github.com/iebb/NekokoLPA2)，本地基线 commit 为 `c2b796a5a90a9b209749988e47ff7fc8e01b673f`，pubspec 为 `2.4.4+604`。它比手机应用新，不等同于手机已安装版本。
- 用户确认目标卡插在安卓手机 SIM 卡槽内，卡自身提供蓝牙广播 `9eSIM000000`，此次不需要外置蓝牙读卡器。此前把所有普通适配卡都归为“必须配外置读卡器”的假设不适用于本目标。

APK 中可直接找到 `NBLESimAdapter`（文件偏移 `0x96263`）、`nble_sim_password_`（`0x305fb`）、完整 UART 服务 UUID（`0xdaf31`，大写形式），以及 `initiateAuthentication`、`authenticateClient`、`getBoundProfilePackage` 字符串。它们与上游对应模块相互印证，但字符串存在并不能证明每条分支的实际执行或两版本行为完全相同。

REA 安卓分析工具因缺少 `REA_JADX_MCP_JAR` 所要求的 JADX headless JAR 无法执行；Dart 快照符号识别也未成功：本 APK 使用 `_kDartSnapshotText`、`_kDartSnapshotData`、`_kDartSnapshotBuildId` 三个新布局符号，而工具当前查找旧的四符号布局。因此此次采用 APK 字节交叉对照、公开源码分析和 Mac 运行发现，不声称已完整反编译 APK，也不声称完成同版本源码重构。

## 2. Mac 已观察到的蓝牙能力

证据文件：[probe-result.json](analysis/9esim-bluetooth/native-probe/probe-result.json)。其中 UTC 时间 `2026-11-06T17:13:04Z` 对应北京时间 2026-11-07 01:13:04。

- macOS 蓝牙权限已允许，发现可连接的 `9eSIM000000`，当次 RSSI 为 **-84 dBm**。
- 已连接并发现服务 `6E400001-B5A3-F393-E0A9-E50E24DCCA9E`，随后主动结束发现流程、断开连接。
- 主机写入特征 `6E400002-B5A3-F393-E0A9-E50E24DCCA9E`：仅支持 **writeWithoutResponse**，不支持 write with response。
- 设备通知特征 `6E400003-B5A3-F393-E0A9-E50E24DCCA9E`：支持 notify，不支持写入或 indicate。
- CoreBluetooth 报告最大 without-response 写入长度 253 字节、with-response 长度 512 字节。后一个数值是连接 API 的能力值，不能覆盖该特征“不支持 write with response”的事实。
- 探针没有认证、没有订阅通知、没有发送 APDU、没有执行 profile 操作；JSON 的 `apdu_writes_performed=false`、`profile_operations_performed=false` 明确记录这一边界。

探针说明与源码在 [native-probe/README.md](analysis/9esim-bluetooth/native-probe/README.md)。其中帧格式已有本地兼容向量测试，但帧编码未接入探针的设备传输。

## 3. 协议分层

整体路径为：Mac 界面／LPA → profile 管理与 ASN.1 请求 → eUICC 逻辑通道 APDU → NBLESim 分帧 → CoreBluetooth GATT → 卡内 BLE 管理入口 → eUICC。

下载新 profile 还需要另一条网络路径：LPA 通过 HTTPS 与运营商 SM-DP+ 进行 ES9+ 交互，再把绑定到目标 eUICC 的 Bound Profile Package（BPP）经 ES10／APDU 装入卡中。蓝牙通道承载 APDU，不能代替 SM-DP+ 授权或生成任意运营商配置。

名称路由见 [ble_adapter.dart:52](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/ble_adapter.dart:52)：六位十六进制后缀的 `9eSIM` 名称进入 NineESIM 类型；[composite_adapter.dart:74](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/composite_adapter.dart:74) 将其映射到 `NBLESimAdapter`。这与八位数字后缀、走 TYBLE 的 B0 Plus 分支不同，不能只凭品牌共用帧格式。

## 4. GATT、认证与会话

UART 服务及两个特征定义见 [nble_sim_adapter.dart:95](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/nble_sim_adapter.dart:95)。连接后先发现服务、注册接收监听、开启 notify，再认证；对应代码从该文件 [262](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/nble_sim_adapter.dart:262) 和 [310](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/nble_sim_adapter.dart:310) 行开始。

上游握手流程见 [924](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/nble_sim_adapter.dart:924) 行：

1. 等待以 `E0` 开头、后接 16 字节的 challenge。
2. 用由设备名称和管理密码派生的密钥进行 Triple-DES ECB 解密。
3. 将明文第 7、8 字节组成的小端 16 位计数器加一，再写回。
4. 重新加密该块，发出 `E0 || ciphertext`，等待认证 ACK；`6A86` 被解释为密码错误。
5. 完成后将 APDU 事务计数器从 0 开始，进入正常收发。

密码归一化、派生和 DES 实现分别在 [1592](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/nble_sim_adapter.dart:1592)、[1614](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/nble_sim_adapter.dart:1614)、[1633](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/nble_sim_adapter.dart:1633) 行。六位十六进制密码直接归一为大写；其他输入使用 CRC24 派生六位十六进制值；名称尾部参与构造认证密钥。本文不复制手机中保存的密码、派生密钥或真实认证报文。

这里的 Triple-DES 是上游的握手认证机制；源码中的普通 NBLE APDU 帧本身是计数器、APDU 和加和校验，不应将整个 BLE 传输描述为该握手算法端到端加密。BPP 的签名、绑定和保护属于另外的 eSIM 远程配置层。

## 5. APDU 分片与恢复

编码入口见 [nble_sim_adapter.dart:1658](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/nble_sim_adapter.dart:1658)：

- 逻辑报文为 `transaction_counter(1) || APDU || sum16(2)`，sum16 对计数器及 APDU 所有字节求和、截取 16 位，按大端附在末尾。
- 每个 BLE 帧增加 1 字节帧头，低 5 位是分片序号，`0x20` 标识最后一片；序号按 32 回绕。
- 上游 native 模式使用最大 101 字节协议帧，web 使用 20 字节；见 [22](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/nble_sim_adapter.dart:22) 和 [1061](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/nble_sim_adapter.dart:1061) 行。不能把探针测得的 253 字节上限直接当成协议允许的所有分片大小。
- 返回帧依次检查首片、分片序号、结束标志、事务计数器、sum16 和 APDU 最小长度；见 [1160](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/nble_sim_adapter.dart:1160) 和 [1703](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/nble_sim_adapter.dart:1703) 行。旧事务的返回包有专门丢弃逻辑。

GATT 写方式必须依据特征能力决定。[ble_transport_utils.dart:14](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/ble_transport_utils.dart:14) 在仅支持 without-response 时选择该方式；[99](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/ble_transport_utils.dart:99) 行按 MTU 再分块，并在 without-response 写之间加入短间隔。本目标已实测只接受 without-response，这是 Mac 实现必须保留的条件。

逻辑通道的普通 ES10 请求通过 STORE DATA APDU（`80 E2 91 00`，CLA 可随逻辑通道映射）发送，入口在 [profile_manager.dart:2081](analysis/9esim-bluetooth/NekokoLPA2/lib/logic/profile_manager.dart:2081)。BLE 连接成功只证明最底层可达，尚不能推出此上所有 APDU 都成功。

## 6. Profile 读取、切换、修改与删除

以下是当前上游实现的能力；其中 EID、空间和配置列表读取已由本次 Mac 界面实测，写入类操作尚未进行：

- **读取列表及状态／名称／ICCID 等元数据**：[profile_manager.dart:269](analysis/9esim-bluetooth/NekokoLPA2/lib/logic/profile_manager.dart:269) 的 `listProfiles` 构建 `ProfileInfoListRequest`，经 APDU 发送并解码结果。EID 和 eUICC 信息入口分别是 [901](analysis/9esim-bluetooth/NekokoLPA2/lib/logic/profile_manager.dart:901) 的 `getEid`、[977](analysis/9esim-bluetooth/NekokoLPA2/lib/logic/profile_manager.dart:977) 的 `getEuiccInfo2`。
- **启用某一配置**：[500](analysis/9esim-bluetooth/NekokoLPA2/lib/logic/profile_manager.dart:500) 的 `enableProfile`；构建 EnableProfileRequest，ICCID 使用半字节交换编码，并携带适配器要求的 refreshFlag。
- **停用配置**：[611](analysis/9esim-bluetooth/NekokoLPA2/lib/logic/profile_manager.dart:611) 的 `disableProfile`；同样考虑 refreshFlag 和当前卡忙状态。
- **修改可写元数据（昵称）**：[723](analysis/9esim-bluetooth/NekokoLPA2/lib/logic/profile_manager.dart:723) 的 `renameProfile`；UTF-8 昵称请求手工 ASN.1 编码在 [2541](analysis/9esim-bluetooth/NekokoLPA2/lib/logic/profile_manager.dart:2541)，使用 `BF29`。
- **删除指定配置**：[772](analysis/9esim-bluetooth/NekokoLPA2/lib/logic/profile_manager.dart:772) 的 `deleteProfile`，按 ICCID 构建 DeleteProfileRequest，并检查 eUICC 返回结果。删除不能被等同于卸载应用；profile 真正位于卡上，也不能假定可从本地数据库恢复。

本目标卡由手机 modem 充当 UICC 终端。上游 [nble_sim_adapter.dart:158](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/nble_sim_adapter.dart:158) 明确 `requiresRefresh=true`：切号后由手机完成 UICC REFRESH／复位，而不是由 Mac 抢答手机的 SIM Toolkit 会话。[638](analysis/9esim-bluetooth/NekokoLPA2/lib/adapter/ble/nble_sim_adapter.dart:638) 的 settle 流程会放开蓝牙连接，避免长时间占用妨碍宿主复位。因此切号时短暂失联可能是正常流程，但仍须重新读状态确认最终结果。

“读取 eSIM 数据”在本方案中指标准接口允许公开的元数据、状态与管理信息，不包括导出 Ki／长期鉴权密钥或将已安装配置克隆成可任意回灌的明文文件。“修改”包括启停、昵称以及接口明确允许的管理项，不包括任意改写运营商签名的 profile 凭据、证书或套餐额度。

## 7. 写入新 eSIM 配置：SM-DP+ 与 BPP

常规安装需要合法激活码／二维码、可用下载授权及网络。界面主路径在 [download_profile_page.dart:555](analysis/9esim-bluetooth/NekokoLPA2/lib/pages/download_profile_page.dart:555)，可复用的服务级串联在 [transfer_downloader.dart:46](analysis/9esim-bluetooth/NekokoLPA2/lib/services/transfer_downloader.dart:46)：

1. 读取 eUICC challenge／info，经 ES9+ `initiateAuthentication` 与 SM-DP+ 启动会话。
2. 卡执行 `authenticateServer`，生成认证响应；客户端经 ES9+ `authenticateClient` 回传。
3. 卡执行 `prepareDownload`；客户端请求 `getBoundProfilePackage`。
4. 客户端将返回的 BPP 分段交给卡的 `loadBoundProfilePackage`，最后处理安装结果／通知及可选启用。

网络三个请求实现在 [es9plus_service.dart:8](analysis/9esim-bluetooth/NekokoLPA2/lib/services/es9plus_service.dart:8)、[25](analysis/9esim-bluetooth/NekokoLPA2/lib/services/es9plus_service.dart:25)、[40](analysis/9esim-bluetooth/NekokoLPA2/lib/services/es9plus_service.dart:40)。卡侧实现分别在 [profile_manager.dart:1685](analysis/9esim-bluetooth/NekokoLPA2/lib/logic/profile_manager.dart:1685)、[1775](analysis/9esim-bluetooth/NekokoLPA2/lib/logic/profile_manager.dart:1775)、[1856](analysis/9esim-bluetooth/NekokoLPA2/lib/logic/profile_manager.dart:1856)。

BPP 传输和 BLE 分帧是两层不同分片：BPP 自身按 ASN.1 段及 MSS 分块，每段通过 STORE DATA `P1=0x11`（后续仍有块）或 `0x91`（段末），`P2` 为块序号发送；详见 [1901](analysis/9esim-bluetooth/NekokoLPA2/lib/logic/profile_manager.dart:1901)。每个 APDU 再经上述 NBLE 帧封装。不能任意插入查询打断安装事务，也不能把返回超时直接理解为未安装而盲目重下。

## 8. Mac 复用路线与本地修改

当前方案复用公开的 Flutter UI、NBLESim 传输和 LPA 核心，通过本机 Xcode 构建 Mac 应用；不需要在安卓手机上安装新的控制程序。Mac 构建时 Rust 初始化被 [main.dart:63](analysis/9esim-bluetooth/NekokoLPA2/lib/main.dart:63) 的 Android 条件排除；Go 原生框架插件只注册 iOS。蓝牙权限声明已在 [Info.plist:44](analysis/9esim-bluetooth/NekokoLPA2/macos/Runner/Info.plist:44) 和 [Release.entitlements:7](analysis/9esim-bluetooth/NekokoLPA2/macos/Runner/Release.entitlements:7)。

本地已移除 SM-DP+ 客户端接受任意 TLS 证书的绕过代码：普通 HTTP 客户端保留系统证书校验，[smdp_client.dart:150](analysis/9esim-bluetooth/NekokoLPA2/lib/services/smdp_client.dart:150)；测试 DNS 连接的 SecureSocket 同样不再接受坏证书，[201](analysis/9esim-bluetooth/NekokoLPA2/lib/services/smdp_client.dart:201)。新增 [smdp_tls_test.dart:8](analysis/9esim-bluetooth/NekokoLPA2/test/services/smdp_tls_test.dart:8) 使用本机自签名服务，确认认证数据发出前拒绝不可信 TLS，服务端请求数为 0；[tls-validation.log](analysis/9esim-bluetooth/tls-validation.log) 记录该测试通过。

可选安装统计默认关闭：字段及未设置时回退值分别位于 [app_settings.dart:258](analysis/9esim-bluetooth/NekokoLPA2/lib/settings/app_settings.dart:258)、[627](analysis/9esim-bluetooth/NekokoLPA2/lib/settings/app_settings.dart:627)；统计插件在 [nekoko_stats_plugin.dart:28](analysis/9esim-bluetooth/NekokoLPA2/lib/plugins/nekoko_stats_plugin.dart:28) 检查该开关。既有应用若曾保存 true，仍须显式关闭；“默认关闭”不代表覆盖所有旧偏好。

## 9. 验证边界与剩余工作

已核实 APK 身份、上游可复用代码、目标卡广播、Mac 蓝牙权限、GATT 连接与特征能力，以及本地 TLS 拒绝测试。完整应用已构建、通过签名核验并在 Mac 认证目标卡；界面显示 EID 尾部 **********、空闲 991.11 KB / RAM 16.23 KB（最终重连读数），并列出 CMI_GDS、Cellfie、Saily 三份配置。尚未实测切号、下载安装、改昵称或删除；这些需要分项验证。

实际验证应先只读连接与读取列表，再选择可恢复的昵称修改；切号须观察手机服务状态并回读卡状态；安装／删除应使用用户指定的可测试配置。不要以已有源码、构建通过、GATT 连接或某一条 SW=9000 代替完整端到端成功证据。

最终产物为 `dist/9eSIM Mac.app`，运行核验见 `runtime-verification.json`。84 项相关上游测试和 1 项新增 TLS 拒绝测试均通过。本文不复制配对密码、激活码、SM-DP+ 匹配码、完整 EID／ICCID、原始 APDU 认证材料或 SIM 长期鉴权密钥。

最终补充：发布版已再次构建、复制并通过深度签名校验，按配置名称的辅助功能入口可用。Mac 上已打开 Saily 详情并读到“已禁用”；配置菜单包含详情、重命名和删除。未提交启停、改名或删除操作。
