# 随身 Wi-Fi 短信能力续分析

更新时间：2026-11-07（Asia/Shanghai）

## 结论

这台 SZXF / MF761W 随身 Wi-Fi 的短信能力分成两层：设备固件和网页管理接口具备完整的收发短信路径；当前外插 9eSIM 已经实测可以接收，但出站短信仍不能算“可用”。两次在 LTE 已注册、数据连接正常时的发送都被异步状态码 `2` 拒绝。

用户随后确认该卡没有购买短信发送额度，只能接收验证码。这与结果吻合，但设备保存的 `Err_Code=0` 没有提供运营商侧的具体拒绝原因，所以不能把它写成从设备证据单独推导出的结论。

## 已证实能力

| 能力 | 证据 | 判断 |
| --- | --- | --- |
| 读取设备收件箱 | `inbound-check.json`，收到 `123456`、`798021` 等 | 已成功 |
| 读取 SIM 收件箱 | `sms_data_total`，`mem_store=0` | 接口可用，当前样本为空 |
| 读取设备短信设置 | `sms-after-first-send.json` | 已成功，短信中心为 `+85290240715`，未修改 |
| 发送 Unicode | `outbound-test.json` | HTTP 受理，异步状态 `2`，未送达 |
| 发送 GSM7 数字 | `outbound-gsm7-test.json` | HTTP 受理，异步状态 `2`，未送达 |
| 读取异步状态 | `cmd_status` 中 `Cmd=4, Cmd_Status=2` | 已成功 |
| 读取本地容量 | 网页源码 `sms_capacity_info`；客户端 `capacity` 命令 | 已实现，表示本地存储容量，不表示套餐额度 |

接收短信的内容实际以 UTF-16BE 十六进制保存，例如 `003100320033003400350036` 解码为 `123456`。发送路径同样使用 UTF-16BE 十六进制；`encode_type` 只告诉固件按 GSM7 或 Unicode 解释，网页并不直接提交已经打包的 GSM 7-bit PDU。

## 发送链路

```text
浏览器 POST /goform/goform_set_cmd_process
  goformId=SEND_SMS
  Number / sms_time / MessageBody / ID / encode_type
        |
        v
goahead -> zte_libwms_* / zte_ufi -> AT SMS/PDU -> 基带 -> 运营商
        |
        v
GET sms_cmd_status_info&sms_cmd=4
  2 = failed, 3 = backend success
```

`SEND_SMS` 返回 `{"result":"success"}` 只代表请求进入设备队列。网页端随后每秒轮询 `sms_cmd_status_info`，状态为 `2` 才显示发送失败，状态为 `3` 才停止轮询。即使状态为 `3`，也只代表设备后端接受，仍不能代替手机到达或状态报告；当前设置 `sms_para_status_report=0`，也没有可用的送达报告。

## 本地数据库给出的额外信息

`device-backup/sms.db` 的关键表和最终快照如下：

- `parameter`：短信中心 `+85290240715`、`Mem_Store=0`、`Send_Retry=0`、状态报告关闭。
- `cmd_status`：初始化命令 `1` 为 `3`；发送命令 `4` 为 `2`，`Err_Code=0`，`Send_Fail_Count=1`。
- `send_content`：只留下最后一次提交的 UTF-16BE 文本，说明请求曾到达短信服务；它不是送达凭证。
- `sms_report`：为空，和关闭状态报告的设置一致。
- `sms`：保存已收到的短信；两次失败出站测试没有形成已送达记录。

`zte_ufi` 字符串还确认了 `AT+CMGS`/PDU、`+CMS ERROR`、短信草稿和失败路径存在；这证明固件实现了通用短信发送机制，但不能证明当前 SIM 套餐允许出站短信。

## 当前客户端改动

`mifi_sms.py` 现在复刻网页端的 GSM7 字符表（包括扩展字符 `[]{}~\\^€|`）、JavaScript UTF-16 单元计数和 UTF-16BE 载荷；`send` 返回 `encoding`、`utf16_units`、`gsm7_extension_count` 和 `outcome`，并新增 `capacity` 命令。纯本地测试位于 `test_mifi_sms.py`，不连接设备、不发送短信。

## 下一次可做的验证

1. 购买或开通该卡的短信发送额度后，只发送一条固定短文本，保留完整的 HTTP 响应、`sms_cmd_status_info`、`cmd_status`、`sms_report` 和接收手机截图。
2. 若状态仍为 `2`，再通过已登录管理接口执行只读 AT 查询 `AT+CSCA?`、`AT+CSMS?`、`AT+CMGF?`、`AT+CPMS?`、`AT+CNMI?`；不要改短信中心或网络配置。
3. 若状态为 `3` 但手机未收到，打开状态报告后再做一次单条测试，区分“后端接受”和“网络送达”。这会改变设置，执行前需要明确授权。

在设备未重新接入 `192.168.0.1` 前，不能进行新的发送验证；目前没有必要刷写固件或绕过网页接口。
