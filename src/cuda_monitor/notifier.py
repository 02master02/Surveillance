"""通知层：微信测试号（公众号测试号）模板消息推送。

刻意只用标准库 urllib —— 这个场景就是两次 HTTP 调用，
引入 requests 只会额外增加目标机的部署成本（要配 venv / 装依赖）。

## 微信 2023 内容规范（写模板前必须先读懂）

《关于规范公众号模板消息的再次公告》（2023-05-04 起生效）会在下发时**自动**处理：

    ① 整行只有一个变量的行，被当成"首行 / 尾部备注"内容删掉
       —— 所以每一行都必须带字面文字，如 "进程：{{p1.DATA}}"
    ② 单个中间主内容不超过 20 字（超出被截断）
    ③ 不支持换行 —— 字段值里 \\n 之后的内容不展示
    ④ 自定义颜色、表情符号被去除

实测教训：把模板写成四行纯 {{xxx.DATA}} 之后，**整条消息正文全空**，
手机上只剩模板标题本身。带 "进程：" 前缀的老版本反而能看到内容。

因此本模块的设计是：

    title      一行标题（带数量）
    p1..pN     每个进程占一行，单行主动裁到 20 字以内，模板里带 "1. " 前缀
    usage      一行摘要

**不要**再往单个字段里拼多行清单 —— 平台会把换行后面的内容丢掉。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .config import WeChat
from .judge import Alert, Decision

TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
SEND_URL = "https://api.weixin.qq.com/cgi-bin/message/template/send"
TEMPLATE_LIST_URL = "https://api.weixin.qq.com/cgi-bin/template/get_all_private_template"
USER_LIST_URL = "https://api.weixin.qq.com/cgi-bin/user/get"
USER_INFO_URL = "https://api.weixin.qq.com/cgi-bin/user/info"

#: access_token 相关的错误码，遇到就刷新令牌重试一次。
TOKEN_ERRCODES = {40001, 42001}

#: 模板里进程行的槽位数默认值。实际取值以 config.wechat.process_slots 为准。
PROCESS_SLOTS = 5

#: 微信 2023 规范：单个中间主内容不超过 20 字，超出会被平台截断。
FIELD_CHAR_LIMIT = 20

#: 模板里每行都带字面前缀（"1. " / "标题：" / "统计："），这些也占字数，
#: 所以字段值本身要再让出这么多，别让整行撞上 20 字上限。
TEMPLATE_PREFIX_CHARS = 3

#: 字段值的安全长度上限。
VALUE_CHAR_LIMIT = FIELD_CHAR_LIMIT - TEMPLATE_PREFIX_CHARS

ERRCODE_HINTS = {
    40003: (
        "OpenID 不合法：先跑 --list-users 拿到微信返回的真实 OpenID，"
        "不要手抄或从截图 OCR —— 这类字符串里 g/q、0/O 极易认错。"
    ),
    40037: "template_id 无效：检查模板 ID 是否复制完整。",
    45009: "接口调用超过日限额。",
    47003: (
        "模板参数不合法：核对 config.json 里字段名与后台模板定义是否逐字一致。"
        "注意模板行必须是「字面文字 + {{字段.DATA}}」，纯变量行会被平台删掉。"
    ),
    43004: "该用户未关注测试号，无法接收模板消息。",
}


def expected_fields(slots: int = PROCESS_SLOTS) -> List[str]:
    """本模块期望后台模板定义的字段名集合。"""
    return ["title"] + [f"p{index}" for index in range(1, slots + 1)] + ["usage"]


class WeChatNotifier:
    def __init__(self, config: WeChat, logger: Optional[logging.Logger] = None) -> None:
        self.config = config
        self.log = logger or logging.getLogger("cuda_monitor.wechat")
        self._access_token: Optional[str] = None
        self._token_expire_at: float = 0.0

    # ---------------- 底层 HTTP ----------------

    def _open(self, request: urllib.request.Request) -> Dict[str, Any]:
        if self.config.proxy:
            handler = urllib.request.ProxyHandler({"http": self.config.proxy, "https": self.config.proxy})
            opener = urllib.request.build_opener(handler)
        else:
            # 不传 proxy 时走系统/环境变量代理，保持默认行为。
            opener = urllib.request.urlopen

        try:
            response = opener(request, timeout=self.config.timeout_sec)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}：{exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"网络不可达：{exc.reason}（检查系统代理 / 防火墙出站规则）") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"请求超时（{self.config.timeout_sec:g}s）") from exc

        with response:
            body = response.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"接口返回非 JSON 内容：{body[:200]}") from exc

    def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        full = f"{url}?{urllib.parse.urlencode(params)}"
        return self._open(urllib.request.Request(full, method="GET"))

    def _post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        return self._open(request)

    # ---------------- 令牌管理 ----------------

    def access_token(self, force_refresh: bool = False) -> Optional[str]:
        now = time.time()
        # 提前 5 分钟视为过期，避免边界上用到失效令牌。
        if not force_refresh and self._access_token and now < self._token_expire_at - 300:
            return self._access_token

        try:
            payload = self._get_json(
                TOKEN_URL,
                {
                    "grant_type": "client_credential",
                    "appid": self.config.app_id,
                    "secret": self.config.app_secret,
                },
            )
        except RuntimeError as exc:
            self.log.error("获取 access_token 失败：%s", exc)
            return None

        token = payload.get("access_token")
        if not token:
            errcode = payload.get("errcode")
            hint = ERRCODE_HINTS.get(errcode, "")
            self.log.error("获取 access_token 被拒绝：errcode=%s errmsg=%s %s", errcode, payload.get("errmsg"), hint)
            return None

        self._access_token = token
        self._token_expire_at = now + int(payload.get("expires_in", 7200))
        self.log.info("access_token 已刷新，有效期 %s 秒", payload.get("expires_in", 7200))
        return token

    # ---------------- 对外接口 ----------------

    def send_template(self, fields: Dict[str, Any], to_user: Optional[str] = None) -> bool:
        """发一条模板消息。

        fields 的 key 必须与后台模板里的 {{key.DATA}} 逐字一致。
        值为空的字段照样发出去 —— 对应那行会显示成光秃秃的前缀，不会消失。
        """
        targets: List[str] = [to_user] if to_user else list(self.config.to_users)
        if not targets:
            self.log.error("没有配置任何接收人（wechat.to_users 为空），消息未发送。")
            return False

        data = {key: {"value": str(value)} for key, value in fields.items()}

        all_ok = True
        for user in targets:
            if not self._send_one(user, data):
                all_ok = False
        return all_ok

    def _send_one(self, user: str, data: Dict[str, Any]) -> bool:
        for attempt in (1, 2):
            token = self.access_token(force_refresh=(attempt == 2))
            if not token:
                return False

            payload = {"touser": user, "template_id": self.config.template_id, "data": data}
            try:
                response = self._post_json(f"{SEND_URL}?access_token={token}", payload)
            except RuntimeError as exc:
                self.log.error("推送请求异常 -> %s：%s", _mask(user), exc)
                return False

            errcode = response.get("errcode", 0)
            if errcode == 0:
                self.log.info("推送成功 -> %s (msgid=%s)", _mask(user), response.get("msgid"))
                return True

            if errcode in TOKEN_ERRCODES and attempt == 1:
                self.log.warning("令牌失效（errcode=%s），刷新后重试一次。", errcode)
                continue

            hint = ERRCODE_HINTS.get(errcode, "")
            self.log.error(
                "推送失败 -> %s：errcode=%s errmsg=%s %s",
                _mask(user),
                errcode,
                response.get("errmsg"),
                hint,
            )
            return False

        return False

    def send_test(self) -> bool:
        """自检用：给第一个接收人发一条测试消息。"""
        if not self.config.to_users:
            self.log.error("未配置接收人，无法发送测试消息。")
            return False
        payload: Dict[str, str] = {"title": "连通性测试", "usage": "链路正常"}
        for index in range(1, self.config.process_slots + 1):
            payload[f"p{index}"] = "测试消息" if index == 1 else ""
        return self.send_template(payload, to_user=self.config.to_users[0])

    def send_alert_batch(self, decision: Decision) -> bool:
        """一条推送列出当前全部超阈值进程。"""
        payload = build_alert_payload(decision, self.config.process_slots)
        if not payload:
            return False
        return self.send_template(payload)

    def send_cleared(self, decision: Decision) -> bool:
        """告警全部解除时推一条收尾通知。"""
        payload = build_cleared_payload(decision, self.config.process_slots)
        if not payload:
            return False
        return self.send_template(payload)

    def find_template(self) -> Dict[str, Any]:
        """从后台拉取当前配置的那个模板定义。"""
        token = self.access_token()
        if not token:
            return {}
        try:
            payload = self._get_json(TEMPLATE_LIST_URL, {"access_token": token})
        except RuntimeError as exc:
            self.log.error("拉取模板列表失败：%s", exc)
            return {}

        for template in payload.get("template_list", []):
            if template.get("template_id") == self.config.template_id:
                return template
        self.log.error("模板列表里找不到 template_id=%s 的模板。", self.config.template_id)
        return {}

    def template_content(self) -> str:
        return self.find_template().get("content", "")

    def list_template_fields(self) -> List[str]:
        """返回后台模板里定义的字段名，用于排查字段不匹配。"""
        template = self.find_template()
        if not template:
            return []

        content = template.get("content", "")
        self.log.info("模板「%s」内容：%s", template.get("title"), content)

        fields: List[str] = []
        # 模板内容形如 进程：{{p1.DATA}}，抠出字段名。
        for chunk in content.split("{{")[1:]:
            head = chunk.split(".DATA", 1)[0].strip()
            if head and head not in fields:
                fields.append(head)
        return fields

    def bare_variable_lines(self) -> List[str]:
        """找出模板里"整行只有变量、没有字面文字"的行。

        这类行会被微信 2023 规范当成首行/备注内容**整行删掉**，
        是"消息正文全空"的头号原因，所以自检里要专门查一次。
        """
        content = self.template_content()
        if not content:
            return []
        offenders: List[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("{{") and stripped.endswith("}}"):
                offenders.append(stripped)
        return offenders

    def render_template(self, values: Dict[str, str]) -> str:
        """把模板内容里的 {{xxx.DATA}} 替换成实际值，得到"手机上看到的样子"。"""
        content = self.template_content()
        if not content:
            return ""
        rendered = content
        for key, value in values.items():
            rendered = rendered.replace("{{" + key + ".DATA}}", value)
        return rendered

    def list_followers(self) -> List[Dict[str, str]]:
        """列出关注了本测试号的用户。

        这是拿到正确 OpenID 的唯一可靠途径 —— 后台页面和截图里的字符串
        存在 OCR 误读风险（g/q、0/O 之类），手抄几乎必错。
        """
        token = self.access_token()
        if not token:
            return []

        try:
            payload = self._get_json(USER_LIST_URL, {"access_token": token})
        except RuntimeError as exc:
            self.log.error("拉取关注者列表失败：%s", exc)
            return []

        if payload.get("errcode"):
            self.log.error(
                "拉取关注者列表被拒绝：errcode=%s errmsg=%s",
                payload.get("errcode"),
                payload.get("errmsg"),
            )
            return []

        followers: List[Dict[str, str]] = []
        for openid in (payload.get("data") or {}).get("openid", []):
            nickname = ""
            try:
                info = self._get_json(
                    USER_INFO_URL, {"access_token": token, "openid": openid, "lang": "zh_CN"}
                )
                nickname = info.get("nickname", "") or ""
            except RuntimeError:
                pass
            followers.append({"openid": openid, "nickname": nickname})
        return followers


def build_alert_payload(decision: Decision, slots: int = PROCESS_SLOTS) -> Dict[str, str]:
    """构造告警推送的字段值。

    单独抽出来是为了能 --preview 预览而不真的发送。
    slots 是模板里 p1…pN 的个数，必须与后台模板一致（自检会校验）。
    """
    alerts = decision.current
    if not alerts:
        return {}

    shown = list(alerts[:slots])
    total_mb = sum(item.used_mb for item in alerts)
    # 用**触发指标**的值，不是显存占比 —— 否则会出现"告警说 4.8%，
    # 但触发线是 15%"这种自相矛盾的消息。
    top = alerts[0].display_percent
    label = alerts[0].metric_label

    if decision.is_first:
        summary = f"新增{len(alerts)}个 最高{top:.0f}%"
    elif decision.added or decision.removed:
        summary = f"增{len(decision.added)}减{len(decision.removed)} 最高{top:.0f}%"
    else:
        summary = f"最高{top:.0f}% 共{total_mb:.0f}MiB"

    payload: Dict[str, str] = {
        "title": f"{label}告警：{len(alerts)} 个进程",
        "usage": _clip(summary),
    }
    for index in range(1, slots + 1):
        payload[f"p{index}"] = _format_process(shown[index - 1]) if index <= len(shown) else ""
    if len(alerts) > slots:
        payload[f"p{slots}"] = f"另{len(alerts) - slots + 1}个未列出"
    return payload


def build_cleared_payload(decision: Decision, slots: int = PROCESS_SLOTS) -> Dict[str, str]:
    """构造"告警解除"推送的字段值。"""
    if not decision.previous:
        return {}

    shown = list(decision.previous[:slots])
    label = shown[0].metric_label if shown else "显存"
    payload: Dict[str, str] = {
        "title": f"{label}告警已结束",
        "usage": _clip(f"持续{_human_duration(decision.duration)}"),
    }
    for index in range(1, slots + 1):
        payload[f"p{index}"] = _format_process(shown[index - 1]) if index <= len(shown) else ""
    if len(decision.previous) > slots:
        payload[f"p{slots}"] = f"共{len(decision.previous)}个已结束"
    return payload


def _format_process(alert: Alert) -> str:
    """把单个进程压成一行，长度不超过 FIELD_CHAR_LIMIT 字。

    模板里那一行本身带 "1. " 前缀，所以这里不再重复序号。
    微信会把超过 20 字的中间内容截断，与其被动截断不如主动裁剪得好看些。

    **让步顺序：百分比 → 目录 → 程序名。PID 永远不丢。**

    真机上同时跑着 20 多个 `python.exe`，只写短名认不出是哪一个；
    而"是哪个进程"只能靠**目录（conda 环境名）+ PID**回答 ——
    百分比在 `usage` 里已经给了本轮最高值，是可让位的那个。

        ① mmdet_py39\\python 11672 86%   27 字
        ② mmdet_py39\\python 11672       23 字
        ③ mmdet_py39 11672 86%           20 字
        ④ mmdet_py39 11672               16 字  ← 真机落在这一档
        ⑤ python 11672 86%               17 字  ← 没有目录可显示时才退回老写法

    每一档都带 PID；`pid <N>` 这种"路径拿不到"的占位不会再拼一次 PID。
    """
    pid = str(alert.pid)
    percent = f"{alert.display_percent:.0f}%"
    labels = alert.labels or (alert.process_name or "?",)

    # 按「名称从最有辨识度到最短」×「先带百分比、再让出百分比」铺开。
    # 注意是内层循环先让出百分比：否则名称一降级就会直接掉到短名，
    # 而短名正是当初用户抱怨"看不出是哪个 python"的东西。
    plans: List[str] = []
    for name in labels:
        # 名称本身就是 "pid 1234" 占位（路径拿不到）时，不再重复拼一次 PID。
        if name.lower().startswith("pid "):
            plans.append(f"{name} {percent}")
            continue
        plans.append(f"{name} {pid} {percent}")
        plans.append(f"{name} {pid}")
    # 兜底：再挤也要把 PID 带上（宁可只有 PID + 百分比）。
    plans.append(f"{pid} {percent}")

    for line in plans:
        if len(line) <= VALUE_CHAR_LIMIT:
            return line
    return _clip(plans[-1])


def _clip(text: str) -> str:
    return text[:VALUE_CHAR_LIMIT]


def _human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}秒"
    if seconds < 3600:
        return f"{int(seconds // 60)}分"
    return f"{seconds / 3600:.1f}时"


def _mask(user: str) -> str:
    """日志里不完整暴露 OpenID。"""
    return user[:6] + "..." if len(user) > 6 else user
