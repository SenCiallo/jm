#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JMComic 每日自动签到
====================

基于 ``jmcomic`` 2.7.7 的**移动端 API 客户端**（``impl="api"``）实现，
既可以在本地手动运行，也可以交给 GitHub Actions 每日定时执行。

核心调用链（与本机 jmcomic 2.7.7 的真实接口一致）::

    option = JmOption.construct({
        "log": True,
        "client": {"impl": "api"},
    })
    client = option.build_jm_client()          # -> jmcomic.JmApiClient (client_key="api")

    login_resp = client.login(username, password)      # 登录，内部写入 cookies / _user_id
    daily_resp = client.get_daily(user_id)             # 签到活动信息（加密响应）
    daily_id = daily_resp.res_data["daily_id"]         # 当前签到活动 ID
    result = client.daily_checkin(daily_id, user_id)   # 打卡
    result.code                                        # 0=签到成功, 1=今日已签到

关于“今日已签到”
----------------
jmcomic 2.7.7 的 ``JmApiClient.daily_checkin`` 只用繁体关键词
（``今天已經簽到過了`` / ``已簽到`` / ``簽到過``）判断重复签到，
而服务端实际会返回简体 ``今天已经签到过了``，此时库会直接抛
``ResponseUnexpectedException``。本脚本做两重兜底：

1. 先用 ``get_daily()`` 的日历判断今天是否已签到，是则跳过打卡请求；
2. 若仍然调用，则捕获异常并按简繁关键词识别为“今日已签到”，按幂等成功处理。

退出码
------
* ``0`` 签到成功，或今日已经签过（幂等，视为成功）
* ``1`` 登录 / 网络 / 签到失败
* ``2`` 配置缺失（账号密码未提供）或依赖缺失

环境变量
--------
==================  ======  ==================================================
变量名              必填    说明
==================  ======  ==================================================
``JM_USERNAME``     是      禁漫账号（用户名 / 邮箱）
``JM_PASSWORD``     是      禁漫密码
``JM_API_DOMAINS``  否      自定义 API 域名，逗号或换行分隔；留空则用库内置域名
``JM_PROXY``        否      代理，``127.0.0.1:7890`` 或 ``clash`` / ``v2ray``
``JM_RETRY_TIMES``  否      单次操作的网络重试次数，默认 ``3``
``JM_LOG``          否      是否打印 jmcomic 内部日志，``0`` 关闭，默认开启
==================  ======  ==================================================

本地运行::

    # Windows PowerShell
    $env:JM_USERNAME = "your_account"; $env:JM_PASSWORD = "your_password"; python signin.py
    # bash
    JM_USERNAME=your_account JM_PASSWORD=your_password python signin.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 常量

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

CST = timezone(timedelta(hours=8))  # 北京时间（固定偏移，避免依赖 tzdata）

USERNAME_ENVS = ("JM_USERNAME", "JM_USER", "JMCOMIC_USERNAME")
PASSWORD_ENVS = ("JM_PASSWORD", "JM_PASS", "JMCOMIC_PASSWORD")

PROJECT_NAME = "JMComic 每日自动签到"


def _force_utf8() -> None:
    """让 stdout/stderr 以 UTF-8 输出，避免 Windows 控制台/CI 下中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8()


# ---------------------------------------------------------------- 日志

class Reporter:
    """统一输出：控制台 + GitHub Actions 注解 + Step Summary。"""

    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.lines: List[str] = []
        self.summary_path = os.getenv("GITHUB_STEP_SUMMARY") or None
        self.is_actions = os.getenv("GITHUB_ACTIONS") == "true"

    def _emit(self, text: str, level: str = "INFO") -> None:
        line = f"[{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {text}"
        self.lines.append(line)
        if not self.quiet or level in ("WARN", "ERROR"):
            print(line, flush=True)
        if self.is_actions and level in ("WARN", "ERROR"):
            cmd = "warning" if level == "WARN" else "error"
            print(f"::{cmd}:: {text}".replace("\n", " "), flush=True)

    def info(self, text: str) -> None:
        self._emit(text, "INFO")

    def warn(self, text: str) -> None:
        self._emit(text, "WARN")

    def error(self, text: str) -> None:
        self._emit(text, "ERROR")

    def group(self, title: str) -> None:
        self.info(f"===== {title} =====")

    def write_summary(self, extra: List[str]) -> None:
        if self.summary_path is None:
            return
        try:
            with open(self.summary_path, "a", encoding="utf-8") as fp:
                fp.write("\n".join(extra) + "\n")
        except Exception as exc:  # summary 写失败不应影响签到结果
            self.warn(f"写入 GITHUB_STEP_SUMMARY 失败: {exc}")


REPORTER = Reporter()


# ---------------------------------------------------------------- 数据结构

@dataclass
class SigninResult:
    status: str = "failed"          # success | already | failed
    message: str = ""
    username: str = ""
    uid: str = ""
    coin: Any = None
    exp: Any = None
    level_name: Any = None
    daily_id: Any = None
    event_name: str = ""
    signed_days: List[str] = field(default_factory=list)
    today_signed: Optional[bool] = None


class AlreadyCheckedIn(Exception):
    """今日已签到：服务端的幂等结果，不是错误。"""


class ServerEmptyData(Exception):
    """服务端返回空 data（形如 ``{"code":200,"data":[]}``）。

    禁漫接口高峰期偶发：HTTP 200、``code`` 也是 200，但 ``data`` 是空数组而不是
    加密的 Base64 密文。jmcomic 2.7.7 ~ 2.7.10 的 ``JmApiResp.decoded_data`` 会把
    list 直接丢给 ``base64.b64decode``，抛出::

        TypeError: argument should be a bytes-like object or ASCII string, not 'list'

    这是服务端瞬时抽风，不是账号或代码问题，重试即可恢复；重试仍失败时按
    「稍后重试」而非「签到失败」上报，避免误判成账号异常。
    """


def is_server_empty_data(message: str) -> bool:
    """识别 jmcomic 把空 data 塞进 base64.b64decode 产生的 TypeError。"""
    text = str(message)
    return (
        "argument should be a bytes-like object or ASCII string" in text
        and "'list'" in text
    )


# 服务端可能用简体或繁体返回“已签到”，而 jmcomic 2.7.7 只内置了部分繁体关键词，
# 这里做一次简繁兜底识别，避免重复运行时误报失败。
ALREADY_SIGNED_KEYWORDS = (
    "今天已经签到", "今天已經簽到",
    "已经签到", "已經簽到",
    "已签到", "已簽到",
    "签到过了", "簽到過了",
    "签到过", "簽到過",
    "已经签过", "已經簽過",
    "已完成",
)


def is_already_checked_in(message: str) -> bool:
    return any(kw in message for kw in ALREADY_SIGNED_KEYWORDS)


# ---------------------------------------------------------------- 工具函数

def mask(value: Optional[str]) -> str:
    """对账号做脱敏，避免日志泄露。"""
    if not value:
        return ""
    if len(value) <= 2:
        return value[0] + "*"
    if len(value) <= 6:
        return value[0] + "*" * (len(value) - 2) + value[-1]
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def parse_domains(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    parts = raw.replace(",", "\n").replace(";", "\n").splitlines()
    return [p.strip().rstrip("/") for p in parts if p.strip()]


def env_first(names: Tuple[str, ...]) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return None


def retry(times: int, base_delay: float, desc: str, func, reporter: "Reporter" = REPORTER):
    """带指数退避的重试包装；禁漫接口偶发抖动时很有用。"""
    times = max(1, times)
    last_exc: Optional[BaseException] = None
    for attempt in range(1, times + 1):
        try:
            return func()
        except AlreadyCheckedIn:
            # 幂等结果，重试没有意义
            raise
        except Exception as exc:  # noqa: BLE001 - 需要捕获库内各种自定义异常
            last_exc = exc
            if attempt >= times:
                break
            delay = base_delay * (2 ** (attempt - 1))
            reporter.warn(f"{desc} 第 {attempt}/{times} 次失败: {exc}；{delay:.0f}s 后重试")
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------- jmcomic 依赖

def load_jmcomic(reporter: Reporter):
    try:
        import jmcomic
        from jmcomic import JmDailyCheckinResp, JmOption, ProxyBuilder
    except Exception as exc:  # pragma: no cover
        reporter.error(f"导入 jmcomic 失败: {exc}")
        reporter.error("请先安装依赖: pip install -r requirements.txt")
        raise SystemExit(EXIT_CONFIG)

    reporter.info(f"jmcomic 版本: {getattr(jmcomic, '__version__', 'unknown')}")
    _patch_jmcomic(jmcomic, reporter)
    return JmOption, JmDailyCheckinResp, ProxyBuilder


def _patch_jmcomic(jmcomic_mod, reporter: Reporter) -> None:
    """在库层面兜住「/daily_chk 返回空 data」的裸 base64 崩溃。

    jmcomic 2.7.7 ~ 2.7.10 的 ``JmApiResp.encoded_data`` 直接返回 ``json()['data']``，
    当服务端给 ``[]`` 时，``decoded_data`` 会把 list 传给 ``base64.b64decode`` 抛
    TypeError。这里替换 ``JmApiResp.res_data``，在解码前先做类型判断，抛出可读的
    ServerEmptyData。
    """
    try:
        from jmcomic import JmApiResp
    except Exception as exc:  # pragma: no cover
        reporter.warn(f"跳过 jmcomic 兼容补丁（导入 JmApiResp 失败）: {exc}")
        return

    if getattr(JmApiResp, "_lcs_patched", False):
        return

    original_res_data = JmApiResp.res_data

    def res_data(self):
        try:
            encoded = self.encoded_data
        except Exception:
            return original_res_data.__get__(self, type(self))
        if isinstance(encoded, list):
            if len(encoded) == 0:
                raise ServerEmptyData(
                    "服务端返回空 data（{\"code\":200,\"data\":[]}），接口瞬时不可用"
                )
            raise ServerEmptyData(f"服务端 data 为非预期数组: {encoded!r}")
        if not isinstance(encoded, str):
            raise ServerEmptyData(f"服务端 data 类型异常: {type(encoded).__name__}")
        return original_res_data.__get__(self, type(self))

    JmApiResp.res_data = property(res_data)
    JmApiResp._lcs_patched = True
    reporter.info("已启用 jmcomic 空 data 兼容补丁")


def build_client(option_cls, proxy_builder, retry_times: int, reporter: Reporter):
    """按约定构造客户端：JmOption.construct({...}).build_jm_client()，impl 固定为 api。"""
    # 默认走移动端 API 客户端（JmApiClient）。若 /daily_chk 长期抽风，可设
    # JM_CLIENT_IMPL=html 切到网页端客户端（签到走 POST /ajax/user_daily_sign，
    # 返回明文 JSON，不经 base64/AES 解码，对空 data 免疫）。
    impl = (os.getenv("JM_CLIENT_IMPL") or "api").strip().lower() or "api"

    conf: Dict[str, Any] = {
        "log": os.getenv("JM_LOG", "1") not in ("0", "false", "False"),
        "client": {
            "impl": impl,                  # api -> JmApiClient / html -> JmHtmlClient
            "retry_times": retry_times,    # 单个请求的域名/网络重试次数
        },
    }
    if impl != "api":
        reporter.info(f"客户端实现: {impl}")

    domains = parse_domains(os.getenv("JM_API_DOMAINS"))
    if domains:
        conf["client"]["domain"] = domains
        reporter.info(f"使用自定义 API 域名: {domains}")

    proxy = (os.getenv("JM_PROXY") or "").strip()
    if proxy:
        proxies = proxy_builder.build_by_str(proxy)
        conf["client"]["postman"] = {"meta_data": {"proxies": proxies}}
        reporter.info(f"使用代理: {proxies}")

    option = option_cls.construct(conf)
    client = option.build_jm_client()
    reporter.info(f"客户端已创建: {type(client).__name__} (client_key={client.client_key})")
    return client


# ---------------------------------------------------------------- 业务步骤

def step_login(client, username: str, password: str, retry_times: int, reporter: Reporter) -> Dict[str, Any]:
    reporter.group("步骤 1/3 登录")

    resp = retry(retry_times, 3.0, "登录", lambda: client.login(username, password), reporter)

    if not resp.is_success:
        raise RuntimeError(f"登录响应异常, HTTP={resp.http_code}")

    data = resp.res_data
    uid = str(data.get("uid", "")) if isinstance(data, dict) else ""

    reporter.info(
        f"登录成功: {data.get('username', username)} "
        f"(uid={uid}, 等级={data.get('level_name', '?')}, "
        f"Jcoin={data.get('coin', '?')}, 经验={data.get('exp', '?')})"
    )
    return data


def step_fetch_daily(client, user_id: Optional[str], retry_times: int, reporter: Reporter):
    reporter.group("步骤 2/3 查询签到活动")

    resp = retry(retry_times, 3.0, "查询签到信息", lambda: client.get_daily(user_id), reporter)

    if not resp.is_success:
        raise RuntimeError(f"签到信息响应异常, HTTP={resp.http_code}")

    data = resp.res_data
    if not isinstance(data, dict):
        raise RuntimeError(f"签到信息格式异常: {type(data).__name__}")

    daily_id = data.get("daily_id")
    if not daily_id:
        # 服务端偶发不返回 daily_id（活动切换/接口抽风）。此时日历信息仍然可用，
        # 不应直接把整条流水线判红，交给 run() 结合日历决定 pending / already。
        reporter.warn(f"签到信息缺少 daily_id，跳过打卡请求: {data}")
        return data, None

    reporter.info(
        f"当前签到活动: {data.get('event_name', '?')} "
        f"(daily_id={daily_id}, 连续进度={data.get('currentProgress', '?')})"
    )
    reporter.info(
        f"奖励: 满3天 Jcoin={data.get('three_days_coin', '?')}/EXP={data.get('three_days_exp', '?')}, "
        f"满7天 Jcoin={data.get('seven_days_coin', '?')}/EXP={data.get('seven_days_exp', '?')}"
    )
    return data, daily_id


def render_calendar(record: Any) -> Tuple[List[str], Optional[bool]]:
    """把 record 周矩阵渲染成日期列表，并判断今天是否已签到。"""
    signed: List[str] = []
    today = datetime.now(CST).strftime("%d")
    today_signed: Optional[bool] = None
    if not isinstance(record, list):
        return signed, today_signed
    for week in record:
        if not isinstance(week, list):
            continue
        for day in week:
            if not isinstance(day, dict):
                continue
            date = str(day.get("date", "")).zfill(2)
            if bool(day.get("signed")):
                signed.append(date)
            if date == today:
                today_signed = bool(day.get("signed"))
    return signed, today_signed


def step_checkin(client, daily_id: Any, user_id: Optional[str], checkin_resp_cls,
                 retry_times: int, reporter: Reporter):
    reporter.group("步骤 3/3 执行签到")

    def _do_checkin():
        try:
            return client.daily_checkin(daily_id, user_id)
        except Exception as exc:  # noqa: BLE001
            # jmcomic 2.7.7 只识别繁体关键词，服务端返回简体“今天已经签到过了”时会抛异常，
            # 这里统一转成 AlreadyCheckedIn，按幂等成功处理。
            if is_already_checked_in(str(exc)):
                raise AlreadyCheckedIn(str(exc)) from exc
            # /daily_chk 返回 {"code":200,"data":[]} 时库内部会裸 b64decode(list)，
            # 转成明确的可重试异常，避免日志里出现难以理解的 TypeError。
            if is_server_empty_data(str(exc)):
                raise ServerEmptyData(
                    "服务端返回空 data（{\"code\":200,\"data\":[]}），签到接口瞬时不可用"
                ) from exc
            raise

    try:
        result = retry(retry_times, 3.0, "签到", _do_checkin, reporter)
    except AlreadyCheckedIn as exc:
        reporter.info(f"今日已签到，无需重复操作 ({exc})")
        return None, "already"
    except ServerEmptyData as exc:
        # 重试耗尽仍然是空 data：服务端抽风，按「稍后重试」处理，不算账号问题。
        reporter.warn(f"签到接口连续返回空数据，本次跳过: {exc}")
        return None, "pending"

    code = getattr(result, "code", None)
    msg = getattr(result, "msg", "") or ""

    if code == checkin_resp_cls.CODE_SUCCESS:
        reporter.info(f"签到成功 {msg}")
        status = "success"
    elif code == checkin_resp_cls.CODE_ALREADY_CHECKED_IN:
        reporter.info(f"今日已签到，无需重复操作 {msg}")
        status = "already"
    else:
        raise RuntimeError(f"未知的签到返回码: code={code}, msg={msg}")

    return result, status


# ---------------------------------------------------------------- 主流程

def run(args, reporter: Reporter = REPORTER) -> SigninResult:
    username = args.username or env_first(USERNAME_ENVS)
    password = args.password or env_first(PASSWORD_ENVS)

    if not username or not password:
        reporter.error(
            "缺少账号或密码。请设置环境变量 "
            f"{'/'.join(USERNAME_ENVS)} 与 {'/'.join(PASSWORD_ENVS)}，"
            "或使用 --username / --password 参数。"
        )
        raise SystemExit(EXIT_CONFIG)

    retry_times = int(os.getenv("JM_RETRY_TIMES", "3") or "3")

    reporter.group(f"{PROJECT_NAME} 开始")
    reporter.info(f"账号: {mask(username)}  时间: {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S %z')}")
    if args.dry_run:
        reporter.info("已启用 --dry-run：只登录并查询签到状态，不执行打卡")

    option_cls, checkin_resp_cls, proxy_builder = load_jmcomic(reporter)

    result = SigninResult(username=mask(username))

    try:
        client = build_client(option_cls, proxy_builder, retry_times, reporter)

        login_data = step_login(client, username, password, retry_times, reporter)
        result.uid = str(login_data.get("uid", ""))
        result.coin = login_data.get("coin")
        result.exp = login_data.get("exp")
        result.level_name = login_data.get("level_name")

        daily_data, daily_id = step_fetch_daily(client, result.uid or None, retry_times, reporter)
        result.daily_id = daily_id
        result.event_name = str(daily_data.get("event_name", ""))
        result.signed_days, result.today_signed = render_calendar(daily_data.get("record"))
        if result.signed_days:
            reporter.info(f"本月已签到日期: {', '.join(result.signed_days)}")

        if args.dry_run:
            result.status = "success"
            result.message = "dry-run 完成（未打卡）"
            reporter.info("dry-run 结束：登录与签到信息查询均正常。")
        elif result.today_signed and not args.force:
            # 服务端日历已标记今日签到，直接按幂等成功处理，省掉一次打卡请求
            result.status = "already"
            result.message = "服务端日历显示今日已签到"
            reporter.group("步骤 3/3 执行签到")
            reporter.info("服务端日历显示今日已签到，跳过打卡请求（加 --force 可强制调用）")
        elif not daily_id:
            # 拿不到 daily_id：无法发起打卡，按 pending 处理（日历已签则视为完成）
            reporter.group("步骤 3/3 执行签到")
            checkin_result, status = None, "pending"
            reporter.warn("缺少 daily_id，本次未发起打卡请求")
            result.status = status
            result.message = "服务端未返回 daily_id，未发起打卡请求"
        else:
            checkin_result, status = step_checkin(
                client, daily_id, result.uid or None, checkin_resp_cls, retry_times, reporter
            )
            if status == "pending":
                # 打卡接口抽风，但服务端日历可信：今日已签则视为完成，否则记为稍后重试。
                if result.today_signed:
                    status = "already"
                    reporter.info("打卡接口异常，但服务端日历显示今日已签到，按已完成处理。")
                else:
                    reporter.warn(
                        "打卡接口异常且日历显示今日未签到；本次未完成，等待下一次定时任务重试。"
                    )
            result.status = status
            if status == "pending":
                result.message = "服务端签到接口连续返回空数据（HTTP 200 但 data=[]）"
            else:
                result.message = getattr(checkin_result, "msg", "") or status

    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"{type(exc).__name__}: {exc}"
        reporter.error(f"签到失败: {result.message}")
        if os.getenv("JM_DEBUG", "0") in ("1", "true", "True"):
            reporter.error(traceback.format_exc())

    return result


def write_summary(reporter: Reporter, result: SigninResult) -> None:
    icon = {"success": "OK", "already": "SKIP", "pending": "WAIT", "failed": "FAIL"}.get(
        result.status, "?"
    )
    title = {
        "success": "签到成功",
        "already": "今日已签到",
        "pending": "服务端异常，等待下次重试",
        "failed": "签到失败",
    }.get(result.status, result.status)

    lines = [
        f"## [{icon}] JMComic 每日签到 - {title}",
        "",
        f"- 时间：`{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S %z')}`",
        f"- 账号：`{result.username}`",
    ]
    if result.uid:
        lines.append(f"- UID：`{result.uid}`")
    if result.level_name is not None:
        lines.append(f"- 等级：`{result.level_name}`　Jcoin：`{result.coin}`　经验：`{result.exp}`")
    if result.event_name or result.daily_id is not None:
        lines.append(f"- 活动：`{result.event_name or '?'}`（daily_id=`{result.daily_id}`）")
    if result.today_signed is not None:
        lines.append(f"- 今日已签：`{'是' if result.today_signed else '否'}`")
    if result.signed_days:
        lines.append(f"- 本月已签到：`{', '.join(result.signed_days)}`")
    if result.message:
        lines.append(f"- 服务端返回：`{result.message}`")

    reporter.write_summary(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signin.py",
        description=f"{PROJECT_NAME}（jmcomic 移动端 API 客户端）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--username", help="禁漫账号，默认读取环境变量 JM_USERNAME")
    parser.add_argument("--password", help="禁漫密码，默认读取环境变量 JM_PASSWORD")
    parser.add_argument("--dry-run", action="store_true",
                        help="只登录并查询签到状态，不执行打卡")
    parser.add_argument("--force", action="store_true",
                        help="即使日历显示今日已签到，也强制调用一次打卡接口")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出警告和错误")
    parser.add_argument("--version", action="version", version="%(prog)s (jmcomic 2.7.7+)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    global REPORTER
    args = build_parser().parse_args(argv)
    REPORTER = Reporter(quiet=args.quiet)

    result = run(args, REPORTER)
    write_summary(REPORTER, result)

    if result.status == "failed":
        REPORTER.error(f"{PROJECT_NAME} 失败")
        return EXIT_FAILED

    if result.status == "pending":
        REPORTER.warn(f"{PROJECT_NAME} 未完成：服务端签到接口异常，等待下次定时任务重试")
        return EXIT_OK

    REPORTER.info(f"{PROJECT_NAME} 完成，状态: {result.status}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())