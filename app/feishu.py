# -*- coding: utf-8 -*-
"""飞书接入：长连接收消息 + 以机器人身份发消息。

为什么用长连接而不是回调：回调要求有公网可达的域名、还要配加密策略；长连接是
"程序主动连上飞书"，家里的电脑、没有公网 IP 也能跑 —— 正好对上本项目
"纯本地、不出网"的前提。

凭证只从本机读：环境变量优先，其次 data/feishu.json（该文件已在 .gitignore 里）。
"""
from __future__ import annotations

import inspect
import json
import os
import re
import threading
import time
import traceback
from collections import OrderedDict
from pathlib import Path

from . import logs
from .config import DATA_DIR

CFG_PATH = DATA_DIR / "feishu.json"
_CFG_LOCK = threading.Lock()


def load_config() -> dict:
    """环境变量优先，其次 data/feishu.json。

    返回 app_id / app_secret / open_id / owner_open_id / chat_ids。老配置只有
    open_id 时把它当成 owner：升级前记下的那个收件人就是主人。
    chat_ids 是 P2 群聊白名单，环境变量用逗号分隔。
    """
    raw: dict = {}
    if CFG_PATH.is_file():
        try:
            raw = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        except ValueError:
            raw = {}
    open_id = os.environ.get("FEISHU_OPEN_ID") or str(raw.get("open_id") or "")
    owner = (os.environ.get("FEISHU_OWNER_OPEN_ID")
             or str(raw.get("owner_open_id") or "")
             or open_id)
    raw_ids = raw.get("chat_ids") or []
    if isinstance(raw_ids, str):
        raw_ids = re.split(r"[,，\s]+", raw_ids)
    chat_ids = [str(x).strip() for x in raw_ids if str(x).strip()]
    env_ids = os.environ.get("FEISHU_CHAT_IDS") or ""
    if env_ids:
        chat_ids = [x.strip() for x in re.split(r"[,，\s]+", env_ids) if x.strip()]
    return {
        "app_id": os.environ.get("FEISHU_APP_ID") or str(raw.get("app_id") or ""),
        "app_secret": os.environ.get("FEISHU_APP_SECRET") or str(raw.get("app_secret") or ""),
        "open_id": open_id,
        "owner_open_id": owner,
        "chat_ids": chat_ids,
        # 云文档：folder_token 为空时建在机器人自己的空间；doc_url_base 用来拼
        # 租户域名（如 https://xxx.feishu.cn/docx/），不填用飞书通用域名。
        "doc_folder_token": (os.environ.get("FEISHU_DOC_FOLDER_TOKEN")
                             or str(raw.get("doc_folder_token") or "")),
        "doc_url_base": (os.environ.get("FEISHU_DOC_URL_BASE")
                         or str(raw.get("doc_url_base") or "")),
    }


def remember_open_id(open_id: str) -> bool:
    """记住 owner：第一次给机器人发消息的人，之后不会被别人改写。

    返回 True 表示这个 open_id 就是 owner（或刚刚成为 owner）。已经配置了
    别的 owner（环境变量或文件）时返回 False，并且不写文件。
    只改 owner_open_id / open_id 两个键，绝不把环境变量里的密钥写回文件。
    """
    open_id = str(open_id or "")
    if not open_id:
        return False
    with _CFG_LOCK:
        raw: dict = {}
        if CFG_PATH.is_file():
            try:
                raw = json.loads(CFG_PATH.read_text(encoding="utf-8"))
            except ValueError:
                raw = {}
        owner = (os.environ.get("FEISHU_OWNER_OPEN_ID")
                 or str(raw.get("owner_open_id") or "")
                 or os.environ.get("FEISHU_OPEN_ID")
                 or str(raw.get("open_id") or ""))
        if owner:
            return owner == open_id
        raw["owner_open_id"] = open_id
        raw["open_id"] = open_id
        CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CFG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        return True


class MessageDeduper:
    """飞书事件会重投：同一条消息只处理一次，只留最近 N 条。

    重投时飞书会给同一个 message_id；连点、网络抖动都可能让同一条命令
    执行两遍（开两个房间、生成两次总结），所以在 spawn 线程之前就拦掉。
    """

    def __init__(self, capacity: int = 500) -> None:
        self.capacity = max(1, int(capacity))
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, message_id: str) -> bool:
        """第一次见到返回 False 并记下；重复投递返回 True。"""
        mid = str(message_id or "")
        if not mid:
            return False
        with self._lock:
            if mid in self._ids:
                self._ids.move_to_end(mid)
                return True
            self._ids[mid] = None
            if len(self._ids) > self.capacity:
                self._ids.popitem(last=False)
            return False

    def __len__(self) -> int:
        with self._lock:
            return len(self._ids)


class Bot:
    """发消息用 REST，收消息用长连接。两者共用一份凭证。"""

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or load_config()
        self.open_id = self.cfg.get("open_id") or ""
        self.owner_open_id = self.cfg.get("owner_open_id") or self.open_id or ""
        self.chat_ids = set(self.cfg.get("chat_ids") or [])
        # 最近 500 个 message_id：飞书重投时不重复执行命令
        self.seen_messages = MessageDeduper(500)
        self.app_id = self.cfg.get("app_id") or ""
        self.app_secret = self.cfg.get("app_secret") or ""
        # 最近一次发送/上传失败的原因，供调用方给用户提示（网络错误或飞书错误码）。
        self.last_error = ""
        if not self.app_id or not self.app_secret:
            raise RuntimeError("缺少飞书凭证：在 data/feishu.json 里填 app_id / app_secret")
        import lark_oapi as lark
        self._lark = lark
        self.client = (lark.Client.builder()
                       .app_id(self.app_id)
                       .app_secret(self.app_secret)
                       # 默认 30 秒；文件上传在弱网下容易踩线，放宽到 60 秒。
                       .timeout(60)
                       .build())

    def admit(self, open_id: str) -> bool:
        """只有 owner 能发命令。还没配置 owner 时，第一个发消息的人成为 owner。

        返回 False 的发送者既不会收到回复，也不能改写推送对象。
        """
        open_id = str(open_id or "")
        if not open_id:
            return False
        if not self.owner_open_id:
            if not remember_open_id(open_id):
                # 文件/环境里刚被别人写入了 owner，以配置为准
                self.owner_open_id = load_config().get("owner_open_id") or ""
                return False
            self.owner_open_id = open_id
            self.open_id = self.open_id or open_id
            logs.log_event("feishu.owner_set", msg="首位发送者成为 owner")
            return True
        if open_id != self.owner_open_id:
            return False
        if not self.open_id:
            self.open_id = open_id
        return True

    def _send(self, msg_type: str, content: str, open_id: str = "",
              *, receive_id_type: str = "open_id") -> bool:
        """发一条消息的公共部分：凭证、收件人、错误日志只写一次。"""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        rid = open_id or self.open_id
        if not rid:
            raise RuntimeError("还不知道发给谁：先在飞书里给机器人发一句话")
        body = (CreateMessageRequestBody.builder()
                .receive_id(rid).msg_type(msg_type).content(content)
                .build())
        req = (CreateMessageRequest.builder()
               .receive_id_type(receive_id_type).request_body(body).build())
        resp = self.client.im.v1.message.create(req)
        ok = bool(resp.success())
        if not ok:
            self.last_error = "code=%s %s" % (resp.code, resp.msg)
            logs.log_event("feishu.send_failed", level="warn", msg_type=msg_type,
                           receive_id_type=receive_id_type,
                           code=resp.code, error=str(resp.msg))
        return ok

    def send_text(self, text: str, open_id: str = "") -> bool:
        """私聊发一条文字。open_id 省略时用记住的那一个。"""
        return self._send("text", json.dumps({"text": text}, ensure_ascii=False), open_id)

    def send_card(self, card: dict, open_id: str = "") -> bool:
        """私聊发一张交互卡片。

        卡片失败（schema 变更 / 字段写错 / 租户没开权限）不抛异常，返回 False，
        由调用方回退纯文本——推送不能因为"好看"而丢消息。
        """
        return self._send("interactive", json.dumps(card, ensure_ascii=False), open_id)

    def send_text_to_chat(self, chat_id: str, text: str) -> bool:
        return self._send("text", json.dumps({"text": text}, ensure_ascii=False),
                          chat_id, receive_id_type="chat_id")

    def send_card_to_chat(self, chat_id: str, card: dict) -> bool:
        return self._send("interactive", json.dumps(card, ensure_ascii=False),
                          chat_id, receive_id_type="chat_id")

    # ---------------------------------------------------------- 文件 / 语音
    def _upload_file(self, path, file_type: str, duration_ms: int = 0) -> str:
        """上传文件拿 file_key。弱网重试 2 次；业务错误不重试。失败返回空串。"""
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody
        err = ""
        self.last_error = ""
        for attempt in range(3):
            try:
                with open(path, "rb") as f:
                    body = (CreateFileRequestBody.builder()
                            .file_type(file_type)
                            .file_name(Path(path).name)
                            .duration(int(duration_ms))
                            .file(f).build())
                    req = CreateFileRequest.builder().request_body(body).build()
                    resp = self.client.im.v1.file.create(req)
                if resp.success():
                    return str(getattr(resp.data, "file_key", "") or "")
                # 参数/权限类错误重试也没用，直接记下来
                err = "code=%s %s" % (resp.code, resp.msg)
                break
            except Exception as e:
                err = str(e)
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                traceback.print_exc()
        self.last_error = err
        logs.log_event("feishu.upload_failed", level="warn",
                       file_type=file_type, error=err)
        return ""

    def send_audio(self, path, duration_ms: int, open_id: str = "") -> bool:
        """私聊发一条语音消息。飞书语音要求 opus，片段控制在 5 分钟内。"""
        key = self._upload_file(path, "opus", duration_ms)
        if not key:
            return False
        content = json.dumps({"file_key": key, "duration": int(duration_ms)},
                             ensure_ascii=False)
        return self._send("audio", content, open_id)

    def send_audio_to_chat(self, chat_id: str, path, duration_ms: int) -> bool:
        key = self._upload_file(path, "opus", duration_ms)
        if not key:
            return False
        content = json.dumps({"file_key": key, "duration": int(duration_ms)},
                             ensure_ascii=False)
        return self._send("audio", content, chat_id, receive_id_type="chat_id")

    def send_file(self, path, open_id: str = "") -> bool:
        """私聊发文件（m4a/zip 用 file_type=stream）。"""
        key = self._upload_file(path, "stream")
        if not key:
            return False
        return self._send("file", json.dumps({"file_key": key}, ensure_ascii=False),
                          open_id)

    def send_file_to_chat(self, chat_id: str, path) -> bool:
        key = self._upload_file(path, "stream")
        if not key:
            return False
        return self._send("file", json.dumps({"file_key": key}, ensure_ascii=False),
                          chat_id, receive_id_type="chat_id")

    # ---------------------------------------------------------- 云文档
    def _doc_url(self, document_id: str) -> str:
        base = (self.cfg.get("doc_url_base") or "https://feishu.cn/docx/").strip()
        return base.rstrip("/") + "/" + str(document_id)

    def _doc_fail(self, what: str, resp=None, error: str = "") -> dict:
        if resp is not None:
            err = "%s：code=%s %s" % (what, resp.code, resp.msg)
        else:
            err = "%s：%s" % (what, error)
        self.last_error = err
        logs.log_event("feishu.docx_failed", level="warn", error=err)
        return {"ok": False, "error": err}

    def _grant_doc(self, document_id: str, open_id: str,
                   perm: str = "full_access") -> dict:
        """把文档授权给指定用户。失败不抛异常，由调用方决定怎么提示。

        应用用 tenant_access_token 建的文档归应用所有，默认不在任何人的
        「我的空间」里；不调这一步，用户点链接只会看到无权限。
        """
        if not document_id or not open_id:
            return {"ok": False, "error": "缺少 document_id 或 open_id"}
        try:
            from lark_oapi.api.drive.v1 import (
                BaseMember, CreatePermissionMemberRequest)
            body = (BaseMember.builder()
                    .member_type("openid").member_id(str(open_id))
                    .perm(perm or "full_access").build())
            req = (CreatePermissionMemberRequest.builder()
                   .token(str(document_id)).type("docx")
                   .need_notification(False).request_body(body).build())
            resp = self.client.drive.v1.permission_member.create(req)
            if resp.success():
                logs.log_event("feishu.docx_shared", document_id=document_id)
                return {"ok": True}
            err = "code=%s %s" % (resp.code, resp.msg)
        except Exception as e:
            traceback.print_exc()
            err = str(e)
        self.last_error = "文档授权失败：" + err
        logs.log_event("feishu.docx_share_failed", level="warn",
                       document_id=document_id, error=err)
        return {"ok": False, "error": err}

    def _share_doc(self, out: dict, document_id: str, share_with: str) -> dict:
        """按需把文档授权给发起人，并补齐 share_* 字段。"""
        out["share_requested"] = bool(share_with)
        out["shared"] = False
        if share_with:
            grant = self._grant_doc(document_id, str(share_with))
            out["shared"] = bool(grant.get("ok"))
            if not grant.get("ok"):
                out["share_error"] = grant.get("error") or ""
        return out

    def create_docx(self, title: str, markdown: str,
                    share_with: str = "") -> dict:
        """把 Markdown 建成一篇飞书云文档，可选授权给发起人。

        先 `documents/blocks/convert` 把 Markdown 转成块，再建文档并用
        `block_descendant` 一次性写入；给了 `share_with` 时再调
        `drive/v1/permission_member` 授权。失败不抛异常，返回 {"ok": False, "error"}，
        调用方降级发 .md 文件——周报不能因为租户没开 docx 权限就整个失效。
        `share_with` 是用户的 open_id；应用建的文档默认只有应用能看，必须授权。
        返回里的 `share_requested` / `shared` / `share_error` 描述授权结果。
        """
        if not str(markdown or "").strip():
            return {"ok": False, "error": "内容为空"}
        try:
            from lark_oapi.api.docx.v1 import (
                ConvertDocumentRequest, ConvertDocumentRequestBody,
                CreateDocumentBlockDescendantRequest,
                CreateDocumentBlockDescendantRequestBody,
                CreateDocumentRequest, CreateDocumentRequestBody)

            conv = (ConvertDocumentRequestBody.builder()
                    .content_type("markdown").content(str(markdown)).build())
            resp = self.client.docx.v1.document.convert(
                ConvertDocumentRequest.builder().request_body(conv).build())
            if not resp.success():
                return self._doc_fail("Markdown 转换失败", resp)
            data = resp.data
            blocks = list(getattr(data, "blocks", None) or [])
            first = list(getattr(data, "first_level_block_ids", None) or [])
            if not blocks or not first:
                return self._doc_fail("Markdown 转换没返回内容", error="empty")

            folder = (self.cfg.get("doc_folder_token") or "").strip()
            body_builder = CreateDocumentRequestBody.builder().title(str(title))
            if folder:
                body_builder = body_builder.folder_token(folder)
            created = self.client.docx.v1.document.create(
                CreateDocumentRequest.builder().request_body(body_builder.build()).build())
            if not created.success():
                return self._doc_fail("创建文档失败", created)
            doc = getattr(created.data, "document", None)
            doc_id = str(getattr(doc, "document_id", "") or "")
            if not doc_id:
                return self._doc_fail("创建文档没返回 document_id")

            desc = (CreateDocumentBlockDescendantRequestBody.builder()
                    .children_id(first).index(0).descendants(blocks).build())
            added = self.client.docx.v1.document_block_descendant.create(
                CreateDocumentBlockDescendantRequest.builder()
                .document_id(doc_id).block_id(doc_id)
                .request_body(desc).build())
            if not added.success():
                # 文档已经建出来但正文没写进去：把链接带回去，至少不是"什么都没留下"。
                # 也要授权，否则用户点开这个空文档同样会撞无权限。
                out = self._doc_fail("写入正文失败", added)
                out["url"] = self._doc_url(doc_id)
                return self._share_doc(out, doc_id, share_with)
            logs.log_event("feishu.docx_created", document_id=doc_id,
                           blocks=len(blocks))
            out = {"ok": True, "document_id": doc_id,
                   "url": self._doc_url(doc_id), "blocks": len(blocks)}
            return self._share_doc(out, doc_id, share_with)
        except Exception as e:
            traceback.print_exc()
            return self._doc_fail("云文档创建异常", error=str(e))


def _nodes_text(node, out: list[str]) -> None:
    """递归掏富文本里的文字和链接。post 消息的正文是嵌套的 tag 树。"""
    if isinstance(node, dict):
        tag = node.get("tag") or ""
        if tag == "a" and node.get("href"):
            out.append(str(node["href"]))
        elif tag in ("text", "code_block", "md") and node.get("text"):
            out.append(str(node["text"]))
        for v in node.values():
            _nodes_text(v, out)
    elif isinstance(node, list):
        for v in node:
            _nodes_text(v, out)


def message_text(msg) -> str:
    """取一条消息里的纯文字。

    只认 text 会漏掉"分享"出来的富文本（前面带一段话、链接在 a 标签里），
    而分享直播间最自然的动作就是转发一条卡片/富文本。
    """
    try:
        content = json.loads(getattr(msg, "content", "") or "{}")
    except ValueError:
        return ""
    if getattr(msg, "message_type", "") == "text":
        return str(content.get("text") or "").strip()
    if getattr(msg, "message_type", "") == "post":
        body = content
        if "content" not in body and "zh_cn" in body:
            body = body.get("zh_cn") or {}
        out: list[str] = []
        if body.get("title"):
            out.append(str(body["title"]))
        _nodes_text(body.get("content"), out)
        return "\n".join(x for x in out if x).strip()
    return ""


_MENTION_RE = re.compile(r"@_user_\d+")


def strip_mentions(text: str) -> str:
    """群聊里 @机器人 会变成 @_user_1 这类占位符；命令前先去掉。"""
    return _MENTION_RE.sub("", text or "").strip()


def mentioned(msg) -> bool:
    """消息里是否 @ 了机器人。text/post 的 mentions 形状不完全一样，两处都认。"""
    raw = getattr(msg, "content", "") or ""
    if "@_user_" in raw:
        return True
    try:
        content = json.loads(raw)
    except ValueError:
        return False
    if isinstance(content, dict) and content.get("mentions"):
        return True
    return False


def _answer(toast: str, card: dict | None = None, kind: str = "info"):
    """卡片按钮的回执：一句 toast +（可选）原地换掉这张卡。"""
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackCard, CallBackToast, P2CardActionTriggerResponse)
    resp = P2CardActionTriggerResponse()
    resp.toast = CallBackToast({"type": kind, "content": toast[:80]})
    if card:
        resp.card = CallBackCard({"type": "raw", "data": card})
    return resp


def actor_open_id(obj) -> str:
    """从消息发送者 / 菜单操作者 / 卡片点击者里取 open_id。

    三处事件模型形状不同：
    - 普通消息：sender.sender_id.open_id
    - 机器人菜单：operator.operator_id.open_id
    - 卡片回调：operator.open_id（扁平，没有 operator_id）
    少认最后一种，卡片按钮就会全部被判成“没有权限”。
    """
    if obj is None:
        return ""
    for attr in ("sender_id", "operator_id", "user_id"):
        sid = getattr(obj, attr, None)
        open_id = getattr(sid, "open_id", "") or ""
        if open_id:
            return str(open_id)
    return str(getattr(obj, "open_id", "") or "")


def run(bot: Bot, on_text, on_card=None, on_menu=None) -> None:
    """建立长连接并阻塞。

    on_text(text, open_id, chat_id="", chat_type="p2p")  收到文字/富文本
    on_card(value, open_id[, chat_id, chat_type]) -> (toast, 卡片)
                                                         点了卡片按钮
    on_menu(key, open_id)                                点了机器人菜单
    卡片回调要求飞书后台把「卡片回传交互」也订阅到长连接上；没配的时候按钮
    静默无反应，所以每张带按钮的卡片都必须自带文字兜底（回复「确认」）。
    """
    lark = bot._lark

    def _handle(data) -> None:
        try:
            event = data.event
            sender = event.sender
            # 其它机器人/应用的消息不处理，避免自问自答
            sender_type = str(getattr(sender, "sender_type", "") or "")
            if sender_type and sender_type != "user":
                return
            msg = event.message
            message_id = getattr(msg, "message_id", "") or ""
            if bot.seen_messages.seen(message_id):
                return
            open_id = actor_open_id(sender)
            chat_id = str(getattr(msg, "chat_id", "") or "")
            chat_type = str(getattr(msg, "chat_type", "") or "p2p")
            is_group = chat_type not in ("", "p2p") and bool(chat_id)
            if is_group:
                # 群聊：只认白名单 chat_id + @机器人；不要求发送者是 owner，
                # 授权边界在“这个群被允许”而不是“每个人都是主人”。
                if chat_id not in bot.chat_ids:
                    logs.log_event("feishu.group_denied", level="info",
                                   chat_id=chat_id[:12], open_id=open_id[:12])
                    return
                if not mentioned(msg):
                    return
            elif not bot.admit(open_id):
                logs.log_event("feishu.not_owner", level="info",
                               open_id=open_id[:12], msg="非 owner 的消息，已忽略")
                return
            text = message_text(msg)
            if is_group:
                text = strip_mentions(text)
            if text:
                # 开线程：一条消息里可能要跟一次短链跳转（最长 5 秒），
                # 卡在收消息的循环里会把整个长连接拖到心跳超时。
                threading.Thread(target=on_text,
                                 args=(text, open_id, chat_id, chat_type),
                                 daemon=True).start()
        except Exception:
            traceback.print_exc()

    def _menu(data):
        try:
            event = data.event
            open_id = actor_open_id(event.operator)
            if not bot.admit(open_id):
                logs.log_event("feishu.not_owner", level="info",
                               open_id=open_id[:12], msg="非 owner 的菜单事件，已忽略")
                return
            key = str(getattr(event, "event_key", "") or getattr(event, "key", "") or "")
            if on_menu is not None and key:
                threading.Thread(target=on_menu, args=(key, open_id),
                                 daemon=True).start()
        except Exception:
            traceback.print_exc()

    def _card(data):
        try:
            event = data.event
            open_id = actor_open_id(event.operator)
            ctx = getattr(event, "context", None)
            chat_id = str(getattr(ctx, "open_chat_id", "")
                          or getattr(event, "open_chat_id", "") or "")
            is_group = bool(chat_id) and chat_id in bot.chat_ids
            # 群聊卡片的授权边界和文字一样：白名单群里的成员都能点；
            # 私聊仍然只有 owner 能点。
            if not is_group and not bot.admit(open_id):
                logs.log_event("feishu.card_denied", level="info",
                               open_id=open_id[:12], chat_id=chat_id[:12],
                               msg="卡片点击者不是 owner")
                return _answer("没有权限", None, "error")
            value = dict(getattr(event.action, "value", None) or {})
            try:
                sig = inspect.signature(on_card)
                n = len([p for p in sig.parameters.values()
                         if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
                var = any(p.kind == p.VAR_POSITIONAL for p in sig.parameters.values())
            except (TypeError, ValueError):
                n, var = 2, False
            if var or n >= 4:
                toast, card = on_card(value, open_id, chat_id,
                                      "group" if is_group else "p2p")
            else:
                toast, card = on_card(value, open_id)
            return _answer(toast, card)
        except Exception as e:
            traceback.print_exc()
            return _answer("出了点问题：%s" % e, None, "error")

    builder = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(_handle))
    if on_card is not None:
        try:
            builder = builder.register_p2_card_action_trigger(_card)
        except Exception:
            # 注册不上（SDK 版本/重复注册）不该让收消息一起停摆
            traceback.print_exc()
    if on_menu is not None:
        try:
            builder = builder.register_p2_application_bot_menu_v6(_menu)
        except Exception:
            traceback.print_exc()
    handler = builder.build()
    client = lark.ws.Client(bot.app_id, bot.app_secret,
                            log_level=lark.LogLevel.INFO, event_handler=handler)
    logs.log_event("feishu.connect", msg="正在建立长连接…")
    client.start()
