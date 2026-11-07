# 04 Python 接口

esim-ble 也可以作为库使用。

- **卡操作是异步的（asyncio），** 都在 `CardService` 里。
- **随身 WiFi 客户端同时提供同步和异步两套接口。** 异步版本在线程里执行同步版本。

```python
import asyncio
from esimble import config
from esimble.config import DATA_DIR
from esimble.db import Database
from esimble.card_service import CardService
from esimble.mifi import Mifi

cfg = config.load()
db = Database(DATA_DIR / "esimble.db")
card = CardService(cfg, db)
mifi = Mifi(cfg.mifi_host, cfg.mifi_password)
```

---

## CardService（`esimble/card_service.py`）

所有方法都通过同一把 `asyncio.Lock` 串行执行，每次调用都会：
1. 新建一次蓝牙会话：扫描、连接、登录，确保 eUICC 模式，打开 ISD-R；
2. 执行操作；
3. 结束后立即断开蓝牙。这样宿主设备能马上处理 SIM 刷新。

构造：`CardService(cfg: Config, db: Database)`

属性：

| 属性 | 说明 |
|---|---|
| `busy: str \| None` | 当前正在执行的操作描述，空闲时为 `None` |
| `last_list: dict \| None` | 最近一次 `refresh()` 的结果 |
| `last_list_at: float` | 最近一次 `refresh()` 的时间戳 |

### 方法

| 方法 | 返回 | 说明 |
|---|---|---|
| `await refresh()` | `dict` | 读 EID、剩余空间、号码列表，并同步到数据库 |
| `await add(activation_code, nickname=None, confirmation_code=None)` | `{"iccid": str}` | 下载安装新号码。卡上已有相同 ICCID 时取消 |
| `await enable(iccid)` | `int` | 启用号码，refreshFlag 为真。返回 0 成功、2 已启用；其他值抛 `CardError` |
| `await disable(iccid)` | `int` | 停用号码，返回值规则同上 |
| `await set_nickname(iccid, nickname)` | `None` | 修改卡上昵称 |
| `await delete(iccid)` | `None` | 删除号码。号码不在卡上，或者正在启用，会抛 `CardError` |
| `session(what)` | 异步上下文管理器 | 直接拿到底层的 `Euicc` 对象，用来做自定义 ES10 操作 |

`refresh()` 返回的结构：

```python
{
  "eid": "89086030…",
  "free_nvm": 1072722,            # 剩余空间，单位字节；卡不报告时为 None
  "profiles": [
    {"iccid": "8985…", "state": "enabled", "name": "WEBBING", "provider": "Saily",
     "nickname": "", "profile_class": "operational"},
  ],
}
```

自定义操作的示例：

```python
async with card.session("custom read") as euicc:
    eid = await euicc.get_eid()
    profiles = await euicc.list_profiles()
```

### 异常

| 异常 | 来源 | 场景 |
|---|---|---|
| `esimble.card.euicc.CardError` | 卡 | 返回错误或非 0 结果。`.sw` 是状态字（可能为 `None`）；6999/6985 表示卡正在切换配置 |
| `esimble.card.es9p.SmdpError` | SM-DP+ | HTTP 错误，或者执行状态不是 `Executed-Success`。消息里带 subjectCode / reasonCode |
| `RuntimeError` / `TimeoutError` | 蓝牙层 | 找不到设备、断线、登录失败（消息里含 `wrong password`）、响应超时 |

---

## Mifi（`esimble/mifi.py`）

构造：`Mifi(host: str, password: str)`

- 第一次调用需要登录的接口时自动登录；
- 会话过期（`loginfo` 不是 `ok`）时自动重新登录一次；
- 没配密码又需要登录时，抛 `MifiError("MiFi admin password not configured")`。

### 方法

每个方法都有同步版本 `xxx_sync()` 和异步版本 `await xxx()`。

| 方法 | 返回 | 说明 |
|---|---|---|
| `status()` | `dict` | 设备状态，字段见 02 文档 `mifi status` |
| `inbox(store=1)` | `list[dict]` | 短信列表，从新到旧。同步版本多一个 `limit=500` 参数 |
| `send(number, text)` | `dict` | 发一条短信，返回 `outcome` 等字段。同步版本多一个 `max_units=70` 参数 |
| `sms_ready()` | `str` | 短信模块状态：`"3"` 正常、`"2"` 失败 |
| `wifi_state()` | `dict` | `{"wifiEnabled": "0"/"1", "SSID1": …}` |
| `set_wifi(enabled: bool)` | `dict` | 打开或关闭 WiFi 热点 |
| `reboot()` | `dict` | 重启随身 WiFi，并清空本地会话 |
| `at(command)` | `str` | 执行 AT 指令，返回原始文本 |

`inbox()` 每条的结构：

```python
{"mifi_id": "6", "peer": "+86166…", "body": "…", "sms_time": "26,11,07,04,54,02,+0",
 "tag": "1", "direction": "in", "status": "unread", "store": 1}
```

### 工具函数

| 函数 | 说明 |
|---|---|
| `sms_encoding(text)` | 返回 `"GSM7_default"` 或 `"UNICODE"`，字符表和网页端一致 |
| `utf16_units(text)` | 按 JavaScript 规则计长度：emoji 算 2 个单元 |
| `decode_content(hex)` | 把设备的 UTF-16BE 十六进制正文解码成文字；不是十六进制时原样返回 |
| `in_service(status)` | 根据 `status()` 判断是否已正常注网 |

### 示例：切号后等待随身 WiFi 恢复

```python
import asyncio, time

async def switch_and_wait(iccid: str, timeout: float = 360):
    await card.enable(iccid)
    await mifi.reboot()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(10)
        try:
            st = await mifi.status()
        except Exception:
            continue                      # 设备还在重启
        from esimble.mifi import in_service
        if st.get("ziccid") == iccid and in_service(st) and await mifi.sms_ready() == "3":
            return st
    raise TimeoutError("MiFi did not come back on the new number")
```

---

## Database（`esimble/db.py`）

SQLite 封装，线程安全（一把可重入锁）。表结构见 05 文档。

构造：`Database(path: Path)`。路径不存在时自动创建，并启用 WAL 模式。

### 号码

| 方法 | 说明 |
|---|---|
| `profiles(include_removed=False)` | 号码列表，按 `priority` 从大到小、再按创建时间排序 |
| `profile(key)` | 按 ICCID、号码（带不带 `+` 都可以）或显示名查找一条 |
| `sync_card_profiles(profiles)` | 把卡上的号码列表合并进表。卡上没有了的标记为 `removed` |
| `update_profile(iccid, **fields)` | 修改字段，见下 |

`update_profile` 可改的字段：
- `label`、`msisdn`、`notes`、`tags`、`poll_enabled`、`priority`、`imsi`；
- `last_active_at`、`last_switch_error`、`last_switch_error_at`；
- `card_state`、`card_nickname`、`on_card`。

示例：给号码补上平台信息。

```python
db.update_profile("89852000000000000011", msisdn="+15550000001", label="Saily US", tags="us,otp")
```

### 短信

| 方法 | 说明 |
|---|---|
| `add_message(**fields)` | 存一条短信，返回新 id。`dedup_key` 重复时返回 `None` |
| `messages(iccid=None, direction=None, since_id=0, limit=50)` | 查询。`since_id > 0` 时按 id 从旧到新返回比它大的记录，否则从新到旧 |
| `max_message_id()` | 当前最大 id |

示例：把随身 WiFi 收件箱里收到的短信存进数据库。

```python
st = await mifi.status()
for m in await mifi.inbox():
    if m["direction"] != "in":
        continue
    db.add_message(
        iccid=st.get("ziccid", ""), msisdn=st.get("msisdn", ""), direction="in",
        peer=m["peer"], body=m["body"], sms_time=m["sms_time"],
        mifi_id=m["mifi_id"], mifi_tag=m["tag"],
        dedup_key=f"{m['mifi_id']}|{m['sms_time']}|{m['peer']}",
    )
```

> 收件箱里的短信不带"属于哪个号码"的信息。上面的例子用当前启用号码的 ICCID 归属短信，只适用于读取时没有切换过号码的情况。

### 其他

| 方法 | 说明 |
|---|---|
| `event(kind, message, level="info")` | 记事件日志。最多保留最近 2000 条 |
| `events(limit=100)` | 读事件日志 |
| `get_setting(key, default=None)` / `set_setting(key, value)` | 键值设置，值用 JSON 序列化 |
| `q(sql, args)` / `one(sql, args)` / `x(sql, args)` | 执行原始 SQL：查询多行、查询一行、执行写操作 |

---

## 底层模块（`esimble/card/`）

需要直接操作协议时再用。

| 模块 | 主要对象 | 说明 |
|---|---|---|
| `transport.py` | `ApduTransport`（Protocol）、`ReplayTransport` | APDU 传输接口：`open()` / `transmit(apdu) -> bytes` / `close()`。换成其他读卡方式（PC/SC、AT 等）时，实现这个接口即可 |
| `nble.py` | `NbleTransport(device_name, password=None)` | 9eSIM 的 BLE 实现，也提供 `encode_frames` / `decode_frames` |
| `euicc.py` | `Euicc(transport, mss=120)` | ES10 命令，见下表 |
| `download.py` | `ActivationCode.parse(text)`、`download_profile(...)` | 激活码解析、完整的 SGP.22 下载流程 |
| `es9p.py` | `Es9pClient(address, insecure=False, ca_file=None)` | ES9+ 的 5 个接口 |
| `tlv.py` | `read` / `children` / `find` / `encode` / `decode_bcd_swapped` … | BER-TLV 编解码 |

`Euicc` 的方法：

| 类别 | 方法 |
|---|---|
| 通道 | `open_isdr()`、`close_isdr()`、`es10(der)` |
| 读取 | `get_eid()`、`get_euicc_info1()`、`get_euicc_challenge()`、`free_nvm()`、`list_profiles()` |
| 下载 | `authenticate_server(...)`、`prepare_download(...)`、`load_bound_profile_package(bpp, progress)`、`cancel_session(...)`、`notification_sent(seq)` |
| 管理 | `enable_profile(iccid, refresh=True)`、`disable_profile(...)`、`delete_profile(iccid)`、`set_nickname(iccid, name)` |

示例：不经过 `CardService`，直接读卡。

```python
from esimble.card.nble import NbleTransport
from esimble.card.euicc import Euicc

async def raw_list():
    t = NbleTransport("9eSIM000000")
    await t.open()
    e = Euicc(t)
    try:
        await e.open_isdr()
        return await e.list_profiles()
    finally:
        await e.close_isdr()
        await t.close()
```
