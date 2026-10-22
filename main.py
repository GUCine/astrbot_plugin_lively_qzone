"""機器人的 QQ 空間：寫說說 → 私信主人審核 → 通過才發。

- 隔一兩天的晚上，按今天群裡發生的事和機器人的心情寫一條草稿，私信主人
- 主人回：發 / 不發 / 重寫 / 改 新內容（用主人的版本發）
- 「說說 自動」後不再審核直接發（發完照樣告訴主人）；「說說 審核」改回來
- 其他指令：寫說說 [主題]、刪說說（刪最近一條機器人發的）、說說 開 / 關 / 狀態
- 發說說的介面參考 Zhalslar/astrbot_plugin_qzone（MIT）：用 NapCat 的 get_cookies 拿空間 Cookie
"""
import asyncio
import json
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import MessageChain

from .lively_common import Settings, T2S, plugin_data_dir

TZ = timezone(timedelta(hours=8))
DRAFT_WINDOW = ("20:30", "21:30")  # 每天這個時段可能寫一條草稿
DRAFT_CHANCE = 0.5  # 每天寫草稿的概率（大約隔天一條）
DRAFT_TTL = 24 * 3600
TUNE_FILE = plugin_data_dir("astrbot_plugin_lively_evolve") / "tunables.json"  # 自我成長插件調的參數


def _tune(key: str, default):
    try:
        v = json.loads(TUNE_FILE.read_text("utf-8"))["values"][key]
        return type(default)(v)
    except Exception:
        return default


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
DELETE_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"

WRITER = """
现在你要发一条 QQ 空间说说，像真人发朋友圈那样：
- 写你今天的小日常、小心情或者一个小感想，可以从下面「今天群里的事」挑一件有意思的说，也可以只写自己（吃白饭、犯懒、尾巴）
- 不要点任何人的名字，不要写 QQ 号，不要提私聊里的事，不要转述别人的隐私
- 1 到 3 句，口语，可以带一两个颜文字或 emoji；不用中文句号「。」；不要加话题标签
- 只输出说说正文，不要解释
"""


def _gtk(p_skey: str) -> str:
    h = 5381
    for ch in p_skey:
        h += (h << 5) + ord(ch)
    return str(h & 0x7FFFFFFF)


def _parse(text: str) -> dict:
    """空間介面返回 JSON / JSONP / 夾在 HTML 裡的 JSON"""
    m = re.search(r"callback\s*\(\s*(\{.*\})\s*\)", text or "", re.I | re.S)
    s = m.group(1) if m else (text or "")[(text or "").find("{"): (text or "").rfind("}") + 1]
    try:
        return json.loads(s.replace("undefined", "null"))
    except Exception:
        return {"code": -1, "message": "无法解析返回内容"}


@register(
    "astrbot_plugin_lively_qzone",
    "GUCine",
    "寫 QQ 空間說說，主人審核後發佈",
    "2.0.0",
)
class LivelyQzone(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.s = Settings(context, config)
        self.dir = Path(StarTools.get_data_dir("astrbot_plugin_lively_qzone"))
        self.data = self.dir.parent
        self.root = self.data.parent
        self.state_file = self.dir / "state.json"
        self.events_file = self.dir / "events.jsonl"
        self.state = self._load()
        self._ctx = None  # (uin, skey, p_skey, 取得時間)
        self._task = asyncio.create_task(self._loop())

    async def terminate(self):
        self._task.cancel()

    # ---------- 狀態 ----------
    def _load(self) -> dict:
        try:
            s = json.loads(self.state_file.read_text("utf-8"))
        except Exception:
            s = {}
        s.setdefault("enabled", True)
        s.setdefault("mode", "review")  # review：先給主人審；auto：直接發
        s.setdefault("draft", None)  # {"text", "ts"}
        s.setdefault("last_draft_day", "")
        s.setdefault("posted", [])  # [{"tid", "text", "ts"}]
        return s

    def _save(self):
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(self.state_file)
        except Exception as e:
            logger.error(f"[lively_qzone] 保存状态失败: {e}")

    def _log(self, kind: str, **data):
        try:
            with self.events_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": int(time.time()), "kind": kind, **data}, ensure_ascii=False) + "\n")
        except Exception:
            pass

    async def _tell_owner(self, text: str):
        try:
            await self.context.send_message(self.s.private_umo(self.s.owner()), MessageChain().message(self.s.owner_text(text)))
        except Exception as e:
            logger.error(f"[lively_qzone] 私信主人失败: {e}")

    # ---------- 空間介面 ----------
    def _client(self):
        return self.s.client()

    async def _login(self, force: bool = False):
        if self._ctx and not force and time.time() - self._ctx[3] < 3600:
            return self._ctx
        client = self._client()
        if client is None:
            raise RuntimeError("QQ 还没连上")
        payload = await client.get_cookies(domain="user.qzone.qq.com")
        raw = str((payload or {}).get("cookies") or "").strip()
        c = {k: v.value for k, v in SimpleCookie(raw).items()}
        uin = c.get("uin", "")
        uin = uin[1:] if uin[:1].lower() == "o" else uin
        if not uin.isdigit():
            raise RuntimeError("拿不到空间登录信息")
        self._ctx = (int(uin), c.get("skey", ""), c.get("p_skey", "") or c.get("skey", ""), time.time())
        return self._ctx

    async def _call(self, method: str, url: str, *, params=None, data=None, retry=True) -> dict:
        uin, skey, p_skey, _ = await self._login()
        params = {"g_tk": _gtk(p_skey), **(params or {})}
        headers = {
            "User-Agent": UA,
            "referer": f"https://user.qzone.qq.com/{uin}",
            "origin": "https://user.qzone.qq.com",
        }
        cookies = {"uin": f"o{uin}", "skey": skey, "p_skey": p_skey}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.request(method, url, params=params, data=data, headers=headers, cookies=cookies) as r:
                res = _parse(await r.text())
                status = r.status
        if retry and (status == 401 or res.get("code") == -3000):
            await self._login(force=True)
            return await self._call(method, url, params=params, data=data, retry=False)
        return res

    async def check(self) -> str:
        """只讀檢查：能不能登入、能不能讀自己的說說"""
        uin, *_ = await self._login(force=True)
        res = await self._call("GET", LIST_URL, params={
            "uin": uin, "ftype": 0, "sort": 0, "pos": 0, "num": 1, "replynum": 0,
            "callback": "_preloadCallback", "code_version": 1, "format": "jsonp", "need_private_comment": 1,
        })
        if res.get("code") not in (0, None):
            return f"空间接口返回 {res.get('code')}：{res.get('message')}"
        return f"空间正常，账号 {uin}，已有说说 {res.get('total', '?')} 条"

    async def publish(self, text: str) -> str:
        uin, *_ = await self._login()
        res = await self._call("POST", PUBLISH_URL, params={"uin": uin}, data={
            "syn_tweet_verson": "1", "paramstr": "1", "who": "1", "con": text,
            "feedversion": "1", "ver": "1", "ugc_right": "1", "to_sign": "0",
            "hostuin": uin, "code_version": "1", "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{uin}",
        })
        if res.get("code") != 0 or not res.get("tid"):
            raise RuntimeError(f"发布失败：{res.get('code')} {res.get('message') or res.get('msg') or ''}")
        tid = str(res["tid"])
        self.state["posted"] = (self.state["posted"] + [{"tid": tid, "text": text, "ts": int(time.time())}])[-20:]
        self._save()
        self._log("posted", tid=tid, text=text)
        return tid

    async def delete(self, tid: str):
        uin, *_ = await self._login()
        res = await self._call("POST", DELETE_URL, data={
            "uin": uin, "topicId": f"{uin}_{tid}__1", "feedsType": 0, "feedsFlag": 0,
            "feedsKey": tid, "feedsAppid": 311, "feedsTime": int(time.time()), "fupdate": 1, "ref": "feeds",
            "qzreferrer": f"https://user.qzone.qq.com/{uin}",
        })
        if res.get("code") != 0:
            raise RuntimeError(f"删除失败：{res.get('code')} {res.get('message') or ''}")
        self._log("deleted", tid=tid)

    # ---------- 寫草稿 ----------
    def _today_bits(self) -> str:
        now = datetime.now(TZ)
        t0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        bits = []
        try:
            con = sqlite3.connect(f"file:{self.data / 'astrbot_plugin_livingmemory' / 'livingmemory.db'}?mode=ro", uri=True)
            for text, meta in con.execute("select text, metadata from documents order by id desc limit 40"):
                m = json.loads(meta or "{}")
                sid = m.get("source_session_id") or m.get("session_id") or ""
                if float(m.get("create_time") or 0) >= t0 and ":GroupMessage:" in sid and m.get("status") == "active":
                    bits.append(f"- 话题：{m.get('topics')}")
            con.close()
        except Exception:
            pass
        recent = "\n".join(f"- {p['text']}" for p in self.state["posted"][-3:])
        return (
            f"现在是 {now.strftime('%m月%d日 %H:%M')}\n"
            + ("今天群里的事：\n" + "\n".join(bits[:6]) + "\n" if bits else "今天群里没什么特别的事\n")
            + (f"你最近发过的说说（别重复）：\n{recent}\n" if recent else "")
        )

    def _persona(self) -> str:
        return self.s.persona_prompt()

    async def write(self, topic: str = "") -> str | None:
        prov = self.s.provider()
        if prov is None:
            return None
        prompt = self._today_bits() + (f"这条说说想写的主题：{topic}\n" if topic else "") + "\n写说说："
        resp = await asyncio.wait_for(prov.text_chat(prompt=prompt, system_prompt=self._persona() + WRITER), timeout=60)
        text = (getattr(resp, "completion_text", "") or "").strip()
        text = re.sub(r"(?:<<|《《|＜＜)[^\n]*", "", text)
        text = re.sub(r"&&[^&]*&&", "", text).replace("。", "").strip().strip('"“”')
        return text[:300] or None

    async def new_draft(self, topic: str = "") -> str:
        text = await self.write(topic)
        if not text:
            return "没写出来，下次再说"
        if self.state["mode"] == "auto":
            tid = await self.publish(text)
            await self._tell_owner(f"〈我〉发了条说说：\n{text}")
            return f"已发布（{tid}）"
        self.state["draft"] = {"text": text, "ts": int(time.time())}
        self._save()
        self._log("draft", text=text)
        await self._tell_owner(
            f"〈我〉想发一条说说～\n\n{text}\n\n"
            "回「发」就发出去，「不发」丢掉，「重写」换一条，「改 你的版本」用你写的发"
        )
        return "草稿已发给主人"

    async def _loop(self):
        await asyncio.sleep(90)
        try:
            logger.info(f"[lively_qzone] {await self.check()}")
        except Exception as e:
            logger.warning(f"[lively_qzone] 空间检查失败: {e}")
        while True:
            try:
                now = datetime.now(TZ)
                day, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
                if self.state["enabled"] and DRAFT_WINDOW[0] <= hm < DRAFT_WINDOW[1] and self.state["last_draft_day"] != day:
                    self.state["last_draft_day"] = day
                    self._save()
                    if random.random() < _tune("qzone.draft_chance", DRAFT_CHANCE):
                        await asyncio.sleep(random.uniform(0, 1800))  # 時段內隨機一個時間
                        logger.info(f"[lively_qzone] {await self.new_draft()}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[lively_qzone] 定时写说说出错: {e}")
            await asyncio.sleep(60)

    # ---------- 主人指令（私信） ----------
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=1450)
    async def owner(self, event: AstrMessageEvent):
        if not self.s.is_owner(event.get_sender_id()):
            return
        text = (event.message_str or "").strip().translate(T2S)
        draft = self.state.get("draft")
        if draft and time.time() - draft.get("ts", 0) > DRAFT_TTL:
            self.state["draft"] = draft = None
            self._save()
        reply, handled = None, False
        edit = re.match(r"^改\s*[：: ]\s*(.+)$", text, re.S)
        try:
            if draft and text in ("发", "发吧", "发布"):
                tid = await self.publish(draft["text"])
                self.state["draft"] = None
                reply = "发出去啦～"
                logger.info(f"[lively_qzone] 主人通过，已发布 {tid}")
            elif draft and text in ("不发", "别发"):
                self.state["draft"] = None
                self._log("rejected", text=draft["text"])
                reply = "好吧，那条不发了"
            elif draft and text in ("重写", "换一条"):
                self.state["draft"] = None
                handled = True
                reply = await self.new_draft()
                reply = None if reply == "草稿已发给主人" else reply
            elif draft and edit:
                mine = edit.group(1).strip()
                tid = await self.publish(mine)
                self.state["draft"] = None
                self._log("edited", before=draft["text"], after=mine)
                reply = "按〈主人〉的版本发出去啦"
            elif text.startswith("写说说"):
                handled = True
                reply = await self.new_draft(text[3:].strip())
                reply = None if reply == "草稿已发给主人" else reply
            elif text == "删说说":
                if not self.state["posted"]:
                    reply = "〈我〉还没发过说说"
                else:
                    last = self.state["posted"].pop()
                    await self.delete(last["tid"])
                    self._save()
                    reply = f"删掉了：{last['text'][:30]}"
            elif text.startswith("说说") and text[2:].strip() in ("自动", "审核", "开", "开启", "关", "关闭", "", "状态"):
                arg = text[2:].strip()
                if arg == "自动":
                    self.state["mode"] = "auto"
                    reply = "以后写好就直接发，发完告诉〈主人〉"
                elif arg == "审核":
                    self.state["mode"] = "review"
                    reply = "以后先给〈主人〉看再发"
                elif arg in ("开", "开启"):
                    self.state["enabled"] = True
                    reply = "会偶尔写说说啦"
                elif arg in ("关", "关闭"):
                    self.state["enabled"] = False
                    reply = "不自己写说说了"
                elif arg in ("", "状态"):
                    try:
                        ok = await self.check()
                    except Exception as e:
                        ok = f"空间连不上：{e}"
                    reply = (
                        f"说说：{'开' if self.state['enabled'] else '关'}，"
                        f"{'直接发' if self.state['mode'] == 'auto' else '先审核'}；"
                        f"〈我〉发过 {len(self.state['posted'])} 条\n{ok}"
                        + (f"\n有一条草稿在等〈主人〉：{draft['text']}" if draft else "")
                    )
        except Exception as e:
            reply = f"出错了：{e}"
            logger.error(f"[lively_qzone] 处理主人指令失败: {e}")
        if reply is not None or handled:
            self._save()
            event.stop_event()
        if reply is not None:
            await event.send(MessageChain().message(self.s.owner_text(reply)))
