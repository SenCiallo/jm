# JMComic 每日自动签到

基于 [jmcomic](https://pypi.org/project/jmcomic/) **2.7.7** 移动端 API 客户端（`impl="api"`）实现的禁漫每日自动签到，
可本地手动运行，也可交给 GitHub Actions 每天定时执行。

- **幂等**：今日已签到不会报错，退出码仍为 `0`
- **可重试**：登录 / 查询 / 打卡都带指数退避重试，抗禁漫接口抖动
- **可观测**：控制台日志 + GitHub Actions 注解 + Step Summary 汇总

---

## 目录结构

```
.
├── signin.py                            # 签到主脚本
├── requirements.txt                     # 依赖（jmcomic==2.7.7）
├── README.md
└── .github/
    └── workflows/
        └── daily-signin.yml             # GitHub Actions 每日定时任务
```

---

## 工作原理

`signin.py` 使用的正是 jmcomic 2.7.7 的真实接口，调用链如下：

```python
from jmcomic import JmOption

# 1. 构造 option，客户端实现固定为移动端 API
option = JmOption.construct({
    "log": True,
    "client": {"impl": "api"},
})
client = option.build_jm_client()            # -> JmApiClient (client_key="api")

# 2. 登录（内部把 cookie 与 _user_id 写入 client）
login_resp = client.login(username, password)
uid = login_resp.res_data["uid"]

# 3. 查询签到活动，取出 daily_id
daily_resp = client.get_daily(uid)
daily_id = daily_resp.res_data["daily_id"]

# 4. 打卡
result = client.daily_checkin(daily_id, uid)
# result.code == 0 -> 签到成功
# result.code == 1 -> 今日已签到
```

`JmDailyCheckinResp` 的返回码含义：

| code | 常量 | 含义 | 脚本退出码 |
| ---- | ---- | ---- | ---------- |
| 0 | `CODE_SUCCESS` | 签到成功，`msg` 内含金币/经验奖励 | 0 |
| 1 | `CODE_ALREADY_CHECKED_IN` | 今日已完成打卡 | 0 |
| 其他 | — | 库会直接抛异常，脚本捕获后打印并退出 | 1 |

---

## 本地使用

### 1. 安装依赖

```bash
python -m pip install -r requirements.txt
```

要求 Python >= 3.9（jmcomic 2.7.7 的 `Requires-Python`），推荐 3.12。

### 2. 设置账号密码

bash / macOS / Linux：

```bash
export JM_USERNAME="你的禁漫账号"
export JM_PASSWORD="你的禁漫密码"
python signin.py
```

Windows PowerShell：

```powershell
$env:JM_USERNAME = "你的禁漫账号"
$env:JM_PASSWORD = "你的禁漫密码"
python signin.py
```

也可以直接用参数（注意会留在 shell 历史里，不推荐）：

```bash
python signin.py --username 账号 --password 密码
```

> Windows 控制台若中文乱码，先执行 `chcp 65001`。

### 3. 先干跑一次

`--dry-run` 只登录并查询签到状态，不会真的打卡，适合确认账号密码和网络是否正常：

```bash
python signin.py --dry-run
```

### 4. 退出码

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 签到成功，或今日已签到 |
| 1 | 登录失败 / 网络失败 / 签到失败 |
| 2 | 未配置账号密码，或未安装 jmcomic |

---

## 环境变量

| 变量名 | 必填 | 默认值 | 说明 |
| ------ | ---- | ------ | ---- |
| `JM_USERNAME` | 是 | — | 禁漫账号（用户名或邮箱） |
| `JM_PASSWORD` | 是 | — | 禁漫密码 |
| `JM_API_DOMAINS` | 否 | 库内置 | 自定义 API 域名，逗号或换行分隔，例如 `www.cdnhjk.net,www.cdngwc.cc` |
| `JM_PROXY` | 否 | 系统代理 | 代理地址，如 `127.0.0.1:7890`，或 `clash` / `v2ray` 快捷写法 |
| `JM_RETRY_TIMES` | 否 | `3` | 单次操作的网络重试次数 |
| `JM_LOG` | 否 | 开启 | 设为 `0` 可关闭 jmcomic 内部日志 |
| `JM_DEBUG` | 否 | `0` | 设为 `1` 时失败会打印完整 traceback |
| `GITHUB_STEP_SUMMARY` | 否 | — | 由 GitHub Actions 自动注入，脚本会写入签到汇总 |

同时兼容别名：`JM_USER` / `JMCOMIC_USERNAME`、`JM_PASS` / `JMCOMIC_PASSWORD`。

---

## 部署到 GitHub Actions

### 1. 推送仓库

把本项目 push 到 GitHub，确保 `.github/workflows/daily-signin.yml` 一起提交。

### 2. 配置 Secrets

仓库页面 → **Settings → Secrets and variables → Actions**：

| 类型 | 名称 | 必填 | 说明 |
| ---- | ---- | ---- | ---- |
| Secret | `JM_USERNAME` | 是 | 禁漫账号 |
| Secret | `JM_PASSWORD` | 是 | 禁漫密码 |
| Variable | `JM_API_DOMAINS` | 否 | 自定义 API 域名 |
| Secret | `JM_PROXY` | 否 | 代理地址（Runner 在国外时一般不需要） |

### 3. 定时规则

Workflow 中的 cron 使用 **UTC** 时间：

```yaml
on:
  schedule:
    - cron: '0 22 * * *'    # 北京时间 06:00
    - cron: '30 22 * * *'   # 北京时间 06:30（兜底重试）
```

> GitHub 的定时任务在仓库 60 天无活动后会自动暂停，需要的话给自己的仓库定期提交一次即可。

### 4. 手动触发

**Actions → JMComic Daily Sign-in → Run workflow**，可勾选 `dry_run` 只做登录验证。

每次运行结束后，job 摘要页会显示签到结果（账号、UID、等级、Jcoin、活动、本月已签到日期）。

---

## 常见问题

**签到失败，提示地区受限 / 403 / 520**
禁漫对部分 IP 地区做了限制。可以在本地挂代理后把代理地址写进 `JM_PROXY`；GitHub Actions 的 Runner 通常可直连，若不行则需要在 Secret 中配置一个可用代理。

**`未能从首页获取到 daily_id`**
网页端客户端（`impl="html"`）才需要抓首页，本项目用的是移动端 API，`daily_id` 由 `get_daily()` 直接返回。若仍报错，一般是接口返回结构变了，请先 `pip install -U jmcomic` 升级。

**域名失效 / 请求超时**
jmcomic 会在创建 API 客户端时自动拉取最新域名。也可以手动指定：`JM_API_DOMAINS="www.cdnhjk.net,www.cdngwc.cc"`。

**CI 报错 `未配置 JM_USERNAME / JM_PASSWORD`**
Secret 名字写错了，或者 Secret 只加在了 Environment 而不是 Repository 上。

**本地 Windows 中文乱码**
脚本会把 stdout 切到 UTF-8；如果终端仍乱码，执行 `chcp 65001` 后重试。

---

### 5. 重复运行与 `--force`

签到是幂等的：**今日已签到不算失败**，脚本会打印「今日已签到，无需重复操作」并以退出码 `0` 结束。

判定顺序：

1. 先看 `get_daily()` 返回的签到日历，若今天已标记为已签到，直接跳过打卡请求；
2. 若仍发起了打卡，则识别服务端的「已签到」提示，同样按成功处理。

> 为什么需要第 2 步：jmcomic 2.7.7 的 `JmApiClient.daily_checkin` 只用繁体关键词
> （`今天已經簽到過了` / `已簽到` / `簽到過`）判断重复签到，而服务端实际返回简体
> `今天已经签到过了`，此时库会抛 `ResponseUnexpectedException`。脚本对该异常做了简繁兜底，
> 避免 GitHub Actions 上第二次定时任务（或手动重跑）误报失败。

想强制调用一次打卡接口（例如排查接口问题时）：

```bash
python signin.py --force
```
---

### 6. 服务端返回空数据（`data=[]`）与 `pending` 状态

**症状**：Actions 里签到失败，日志出现

```
[WARN] 签到 第 1/3 次失败: argument should be a bytes-like object or ASCII string, not 'list'
[ERROR] 签到失败: TypeError
```

**原因**：`/daily_chk` 偶发返回 `{"code":200,"data":[]}`（服务端瞬时异常或活动切换）。
jmcomic 2.7.7 ~ 2.7.10 的 `JmApiResp.decoded_data` 会把 `data` 原样交给
`JmCryptoTool.decode_resp_data`，里面直接 `base64.b64decode(data)`，遇到 list 就抛 `TypeError`。
登录、`/daily` 都正常，只有打卡这一步炸——**与账号密码、代理、地区限制无关**，重跑往往就好了。

**处理**：脚本已做三层兜底，这类情况不再判红。

1. **库层拦截**：启动时给 `JmApiResp.res_data` 打补丁，`data` 是空 list / 非字符串时抛可读的
   `ServerEmptyData`，日志里不再出现裸 `TypeError`。
2. **识别并重试**：`step_checkin` 把该异常当可重试错误，按 3s / 6s 退避重试（`JM_RETRY_TIMES` 控制次数）。
3. **降级为 `pending`**：重试耗尽后不抛错，状态记为 `pending`，job 摘要显示
   `[WAIT] 服务端异常，等待下次重试`，**退出码 0，CI 保持绿色**；当天 06:30 的第二次定时任务会自动补签。

另外，若 `/daily` 没返回 `daily_id`（同样会导致无法打卡），也走 `pending` 而不是直接失败；
如果此时日历已标记今日签到，则直接按 `already` 成功处理。

**兜底方案**：若某天 API 端持续抽风，可切到网页端客户端（签到走 `POST /ajax/user_daily_sign`，
返回明文 JSON，不经 base64/AES 解码，对空 data 天然免疫）：

```bash
JM_CLIENT_IMPL=html python signin.py
```

在 Actions 里把它加到 `daily-signin.yml` 的 `env:` 下即可。
## 免责声明

本项目仅用于个人账号的自动化签到，不下载、不传播任何内容。请遵守禁漫的用户协议与当地法律法规，
因使用本脚本产生的任何后果由使用者自行承担。账号密码仅保存在你自己的环境变量或 GitHub Secrets 中，
脚本不会上传或记录明文密码（日志里账号已做脱敏）。