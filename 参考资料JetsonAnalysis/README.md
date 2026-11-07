# USB 随身 Wi-Fi / 9eSIM 短信实测

## 最终结果

- 已让设备从内置卡切到外插 9eSIM；读取到目标号码 +15550000001。
- 已成功注册中国联通 LTE 漫游，数据连接正常。
- 接收已验证：用户从 +86138xxxxxxxx 发来未知数字，设备收件箱读取到 **123456**（id=4，tag=1）。
- 发送未成功：分别提交一条 Unicode 和一条 GSM7 六位数字测试，均返回异步失败，首条经用户确认未收到。未自动重发。
- 用户随后核实：该卡未购买短信发送额度，只能接收验证码。这与实测结果一致，但并未取得运营商拒绝的具体原因码；数据库 Err_Code=0，不能单独据此归因。
- 当前无需自写或刷写固件。购买发送额度后仍需重新验证出站送达，不能将现有接口支持等同于已验证发送成功。
- 已按授权恢复 AT+ZMODE=0 并重启；最终 ADB 列表为空。外插卡仍为 sim / sim_switch=0，锁状态 unlock，LTE 数据连接恢复。

## 硬件与运行架构

实机证据来自 USB 描述符、AT 查询、ADB 只读系统信息和导出的程序：

- USB 名称 SZXF Mobile Boardband，VID:PID 19d2:1557。普通模式为 CDC ECM，macOS 通过 en10 与设备通信；管理地址 http://192.168.0.1/index.html。
- /proc/cpuinfo 的 Hardware 为 TSP ZX297520V3；AT+CGMI 返回 XFNJ，AT+CGMM 返回 7520V3_MIFI。
- 硬件版本 MF761W_V1.1_OM_QX；固件 XFNJ_P42_U19_MF761W_V1.1_OM_QX_YM_3ES3_P006。
- Linux 3.4.110-rt140，armv7l；BusyBox 1.21.0。导出的服务程序为 ARM 32 位 ELF，使用 uClibc 动态链接。
- 内核 MemTotal=22148 kB，这是 Linux 可见内存，不能当作板载物理 RAM 容量。
- /proc/mtd 共六个分区，总计 8 MiB：zloader、nvrofs、uboot、imagefs、rootfs、userdata。rootfs 为只读 SquashFS，userdata 为可写 JFFS2。
- Wi-Fi 驱动线程为 aicwf_*，固件目录含 8800dc 系列文件，提示 AIC Wi-Fi 方案；未拆板核实精确芯片型号。
- SIM 配置 sim_switch_number=3，sim_switch_gpio=GPIO38+AT；观察到内置 esim、esim2 和外插 sim 三种状态。

通信路径：Mac → USB ECM → GoAhead HTTP/goform → zte_ufi 短信与 AT 服务 → 基带/UICC → 外插 9eSIM 的当前配置 → 运营商网络。

设备进程包含 goahead、zte_ufi、hostapd、udhcpd、dnsmasq。zte_ufi 打开多个 /dev/rpm* 通道及 /dev/cpnv，使用 /mnt/userdata/etc_rw/sms.db 存储短信；具体通道协议未完整反编译。imagefs 中 cpuap_*.lzma / cpuphy_*.lzma 提示独立的协议及物理层固件分区内容，但未分析其全部实现。

## 本次修复

最初设备使用内置卡，No Service / Limited Service，lock_network_status=lock。原生 SIM_SWITCH 请求被拒绝。用户明确授权后，临时启用 AT+ZMODE=1 并重启，取得本机 ADB 调试连接。

已私密保存 NV 配置快照，从设备自身配置读取准确的 unlock_network_code，通过固件原生 UNLOCK_NETWORK 接口提交一次。设备返回成功并重启，随后自动使用外插卡，号码与用户提供的一致；解锁剩余次数仍为 5。未猜码、未刷写分区、未修改 IMEI、未手动改短信中心。

调试接口最终已恢复原模式并重启核验。解锁状态保留，以便继续使用用户的外插卡。

## 已恢复并实测的接口

- GET /goform/goform_get_cmd_process：cmd=loginfo、设备状态、短信设置和收件箱。
- POST /goform/goform_set_cmd_process：goformId=LOGIN，本版本使用 Base64 编码管理密码。
- cmd=sms_cmd_status_info&sms_cmd=1：短信初始化；结果 3 表示完成。
- cmd=sms_data_total&page=0&data_per_page=500&mem_store=1&tags=10&order_by=order by id desc：设备短信列表；mem_store=0 查询 SIM 存储。
- cmd=sms_parameter_info：短信中心与存储设置。目标卡读得短信中心 +85290240715，未修改。
- cmd=sms_capacity_info：网页端用它统计 NV/SIM 的总容量、接收、发送和草稿占用；它只反映设备本地存储，不代表运营商发送额度。
- goformId=SEND_SMS：Number、sms_time、MessageBody、ID、encode_type；MessageBody 为 UTF-16BE 十六进制文本，编码类型按原网页选择 GSM7_default / UNICODE。
- cmd=sms_cmd_status_info&sms_cmd=4：异步发送结果，2=失败、3=后端成功。HTTP success 仅表示受理，不能据此宣称送达。
- goformId=EXECUTE_AT_COMMAND&at_cmd=...：AT 桥接可用；普通模式下仍可通过已登录的本地管理接口调用。

## 本地客户端

Python 3.9+，无需第三方包；管理密码交互输入，不保存：

```sh
python3 analysis/mifi-sms/mifi_sms.py --login status
python3 analysis/mifi-sms/mifi_sms.py --login inbox
python3 analysis/mifi-sms/mifi_sms.py --login settings
python3 analysis/mifi-sms/mifi_sms.py --login capacity
```

取得发送额度后，才进行新的出站测试：

```sh
python3 analysis/mifi-sms/mifi_sms.py --login send +86138xxxxxxxx 'MIFI SMS TEST'
```

客户端不自动重发、不删除短信、不标记已读、不修改网络配置。它复刻网页端的 GSM7 字符表和 UTF-16BE 载荷，并在发送结果中报告编码、UTF-16 单元数、GSM7 扩展字符数及异步 outcome。为避免产生长短信和额外计费，诊断发送仍限制为单条 1–70 个 UTF-16 单元；超过 500 条的收件箱分页暂未实现。无服务、受限服务及搜索网络时拒绝发送。

可先查询设备本地短信容量：

```sh
python3 analysis/mifi-sms/mifi_sms.py --login capacity
```

本轮静态分析确认，`goahead` 只把 `sms_cmd_status_info` 的 `Cmd_Status` 暴露给网页；`zte_ufi` 的本地数据库还保存 `Err_Code`、失败计数和 `send_content`。本次两条出站测试的 `Err_Code` 都是 `0`，状态均为 `2`，且没有生成已发送短信记录，因此现有证据只能证明设备/运营商异步拒绝，不能从设备错误码单独识别“未购买短信额度”。用户对卡套餐的核实是目前最具体的原因证据。

验证：Python 语法检查、实机登录、状态与设置读取、设备/SIM 短信读取、入站数字验证、两次受控出站失败状态读取。购买额度后的出站送达尚未验证。

## 证据文件与边界

- final-verification.json：调试关闭、ADB 移除、外插卡解锁、目标号码与 LTE 状态的最终核验。
- inbound-check.json：入站 123456 的原始管理 API 结果。
- after-unlock.json、sms-after-first-send.json、debug-restoration.json：修复与短信测试记录。
- device-backup/：权限受限的 NV 快照、短信库及导出程序；包含卡标识和敏感设备配置，未在本文复制密码或解锁码。
- zte_ufi 导出 SHA-256：d0b4f404ae20d805b0ca749bb7b626ea75a1ce8f149afe4060a4d02a03549fc5。zte_ufi 与 goahead 的导出均通过设备侧 MD5 比对。
- REA 前端分析父证据 ev_681e071e8110f1c644e640abb7f72a6b6cc58fa2dbbef62078ca6e2afa2748c0，覆盖 com.js/set.js/sim_device.js；图覆盖部分，不能单凭静态分析证明运行成功。原生程序已绑定分析，未完成全部调用链反编译。
- 旧 query-*.json 和 test-baseline.json 是修复前状态，不代表最终结果。

自动审批历史：启用调试最初因缺少明确授权被拒绝；用户明确授权后成功执行并在结束时恢复。另有候选 AT+ZSET="VOICE_SUPPORT" 因可能修改配置被拦截，未执行，已从诊断查询中移除。
