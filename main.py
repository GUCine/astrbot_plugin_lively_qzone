"""機器人的 QQ 空間：寫說說 → 私信主人審核 → 通過才發。

- 隔一兩天的晚上，按今天群裡發生的事和機器人的心情寫一條草稿，私信主人（10-26 起定時寫關著，見交接說明）
- 草稿給主人看（10-26 用戶定插件不寫固定句）：她寫一句（人設＋草稿這件事），再發一張【后台】卡片（正文、配圖、從哪來的）；
  主人怎麼回都交給她，她用工具 lively_qzone_review 去發、不發、改了再發、丟掉重寫（只有主人說了才做得成），
  沒有「發 / 不發 / 重寫 / 改」固定指令了。面板信箱照舊能處理
- 配不配圖機器人自己決定（覺得有畫面才配）：要配就自己定畫面，借群漫畫插件的畫圖配置畫好，連圖一起給主人審（配圖的一定先審）
- 「寫說說 配圖／不配圖 [主題]」只當成主人的想法帶給她，配不配還是她定（10-26 起，以前是硬性指定）
- 圖存在插件資料目錄，記錄（events.jsonl 的 draft/posted/rejected）帶圖檔名，面板的說說記錄照著顯示
- 「說說 自動」後不再審核直接發（發完照樣告訴主人）；「說說 審核」改回來
- 她自己發（10-26）：工具 lively_qzone_post（聊天時）和心跳的 qzone_post 動作，什麼時候發、寫什麼、配不配圖她定，
  配圖可以用她在這個聊天裡剛畫的那張；不管自動還是審核模式，一律先給主人審，一天最多送 HER_DAY_MAX 條。
  主人回了之後（私信或面板），結果記進她發起的那個聊天，下一輪她就知道
- 後台指令（回覆是【后台】白話）：刪說說（刪最近一條機器人發的）、說說 開 / 關 / 自動 / 審核 / 狀態。
  「寫說說」10-26 拿掉了：主人想要她寫一條就跟她說，她有 lively_qzone_post
- 發說說的介面參考 Zhalslar/astrbot_plugin_qzone（MIT）：用 NapCat 的 get_cookies 拿空間 Cookie
"""
import asyncio
import base64
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

from .lively_common import ASK_OWNER, Settings, T2S, note_outbox, pick_final, plugin_data_dir, tell_owner

TZ = timezone(timedelta(hours=8))
DRAFT_WINDOW = ("20:30", "21:30")  # 每天這個時段可能寫一條草稿
DRAFT_CHANCE = 0.5  # 每天寫草稿的概率（大約隔天一條）
DRAFT_TTL = 24 * 3600
HER_DAY_MAX = 3  # 她自己一天最多送幾條說說給主人審
DRAWN_TTL = 2 * 3600  # 「刚画的」：這個聊天裡多久以內畫的
# 「刚画的」去問哪些畫圖插件（插件 ID, 方法名），取最新的那張
DRAWN_FROM = [("astrbot_plugin_lively_comic", "recent_drawn")]
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
UPLOAD_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"

WRITER = """
现在你要发一条 QQ 空间说说，像真人发朋友圈那样：
- 写你今天的小日常、小心情或者一个小感想，可以从下面「今天群里的事」挑一件有意思的说，也可以只写自己（吃白饭、犯懒、尾巴）
- 不要点任何人的名字，不要写 QQ 号，不要提私聊里的事，不要转述别人的隐私
- 1 到 3 句，口语，可以带一两个颜文字或 emoji；不用中文句号「。」；不要加话题标签
- 只输出说说正文，不要解释，不要先写分析、清单或「写：」这类草稿
"""
IMAGE_HOW = "正文写完后另起一行，用「配图：」开头，用英文写画面（主体、动作、场景、画风，例如 cute anime illustration），不要让图里出现文字；画面里有你自己就在这行最后加上 [me]"
IMAGE_RULE = f"""
- 这条说说要配一张图，图由你自己决定：{IMAGE_HOW}
"""
IMAGE_MAYBE = f"""
- 配不配图你自己决定，像真人发朋友圈那样：有个画面能让这条更有意思（在吃什么、在干嘛、窝在哪、今天的样子）就配一张，没什么好画的就不配，别为了配而配。要配就{IMAGE_HOW}；不配就不写这一行
"""
IMAGE_LINE_RE = re.compile(r"^\s*配图\s*[:：]\s*(.+)$", re.M)


def _gtk(p_skey: str) -> str:
    h = 5381
    for ch in p_skey:
        h += (h << 5) + ord(ch)
    return str(h & 0x7FFFFFFF)


def _pic_name(path) -> str | None:
    """記錄裡只存圖檔名（圖在插件資料目錄），面板照名字取圖"""
    return Path(path).name if path else None


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
    "2.1.0",
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
        self._cookies: dict = {}  # 空間的整份 Cookie，傳圖時用
        self._tasks: set = set()
        self._task = asyncio.create_task(self._loop())

    async def terminate(self):
        self._task.cancel()
        for t in list(self._tasks):
            t.cancel()

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

    def _notify(self, facts: str, what: str, must: bool = False, card: str = "", image: str | None = None):
        """告訴主人（在背景問她要說什麼，不卡住工具和指令）：插件不寫冒充她口吻的句子"""
        async def run():
            try:
                said = await tell_owner(self.s, facts, what, must=must, card=card, image=image, kind="说说")
                self._log("report", what=what, said=said[:300])
            except Exception as e:
                logger.error(f"[lively_qzone] 告诉主人「{what}」失败: {e}", exc_info=True)
        t = asyncio.create_task(run())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _ask_owner(self, text: str, pic: str | None, source: str = ""):
        """草稿給主人看：她寫一句＋【后台】卡片（正文、配圖、從哪來的）"""
        her = self.s.fill(source).replace(self.s.me(), "她") if source else ""
        self._notify(
            "〈我〉想发一条 QQ 空间说说，在等〈主人〉看（〈主人〉同意了才发出去）\n"
            f"正文：{text}\n" + ("配了一张图" if pic else "不配图") + (f"\n{source}" if source else ""),
            "说说草稿等主人看", must=True, image=pic,
            card="说说草稿 · 等你定" + ("（配下面这张图）" if pic else "") + f"\n「{text}」" + (f"\n来源：{her}" if her else ""),
        )

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
        self._cookies = c
        uin = c.get("uin", "")
        uin = uin[1:] if uin[:1].lower() == "o" else uin
        if not uin.isdigit():
            raise RuntimeError("拿不到空间登录信息")
        self._ctx = (int(uin), c.get("skey", ""), c.get("p_skey", "") or c.get("skey", ""), time.time())
        return self._ctx

    async def _call(self, method: str, url: str, *, params=None, data=None, retry=True, timeout=30, full_cookies=False) -> dict:
        uin, skey, p_skey, _ = await self._login()
        # g_tk 放最後：登入過期重試時傳進來的 params 裡帶著舊的，要用新登入算的蓋掉
        params = {**(params or {}), "g_tk": _gtk(p_skey)}
        headers = {
            "User-Agent": UA,
            "referer": f"https://user.qzone.qq.com/{uin}",
            "origin": "https://user.qzone.qq.com",
        }
        cookies = {"uin": f"o{uin}", "skey": skey, "p_skey": p_skey}
        if full_cookies:
            cookies = {**self._cookies, **cookies}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.request(method, url, params=params, data=data, headers=headers, cookies=cookies) as r:
                res = _parse(await r.text())
                status = r.status
        if retry and (status == 401 or res.get("code") == -3000):
            await self._login(force=True)
            return await self._call(method, url, params=params, data=data, retry=False, timeout=timeout, full_cookies=full_cookies)
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

    async def _upload(self, path: str) -> dict:
        """傳一張圖到空間相冊，回傳發說說要帶的 richval、pic_bo"""
        uin, _, p_skey, _ = await self._login()
        g_tk = _gtk(p_skey)
        res = await self._call("POST", UPLOAD_URL, timeout=120, full_cookies=True, data={
            "filename": "filename", "uploadtype": "1", "albumtype": "7", "exttype": "0",
            "zzpaneluin": uin, "p_uin": uin, "uin": uin, "output_type": "json", "refer": "shuoshuo",
            "charset": "utf-8", "output_charset": "utf-8", "upload_hd": "1",
            "hd_width": "2048", "hd_height": "10000", "hd_quality": "96",
            "backUrls": "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
                        "http://119.147.64.75/cgi-bin/upload/cgi_upload_image",
            "url": f"{UPLOAD_URL}?g_tk={g_tk}", "base64": "1",
            "picfile": base64.b64encode(Path(path).read_bytes()).decode(),
        })
        d = res.get("data") or {}
        if res.get("ret") != 0 or "&bo=" not in str(d.get("url", "")):
            raise RuntimeError(f"传图失败：{res.get('ret', res.get('code'))} {res.get('msg') or res.get('message') or ''}")
        return {
            "richtype": "1",
            "richval": f",{d['albumid']},{d['lloc']},{d['sloc']},{d['type']},{d['height']},{d['width']},,{d['height']},{d['width']}",
            "pic_bo": str(d["url"]).split("&bo=")[1],
        }

    async def publish(self, text: str, image: str | None = None) -> str:
        uin, *_ = await self._login()
        pic = await self._upload(image) if image else {}
        res = await self._call("POST", PUBLISH_URL, params={"uin": uin}, full_cookies=bool(pic), data={
            "syn_tweet_verson": "1", "paramstr": "1", "who": "1", "con": text,
            "feedversion": "1", "ver": "1", "ugc_right": "1", "to_sign": "0",
            "hostuin": uin, "code_version": "1", "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{uin}", **pic,
        })
        if res.get("code") != 0 or not res.get("tid"):
            raise RuntimeError(f"发布失败：{res.get('code')} {res.get('message') or res.get('msg') or ''}")
        tid = str(res["tid"])
        row = {"tid": tid, "text": text, "ts": int(time.time())}
        if image:
            row["image"] = _pic_name(image)
        self.state["posted"] = (self.state["posted"] + [row])[-20:]
        self._save()
        self._log("posted", tid=tid, text=text, image=_pic_name(image))
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

    async def write(self, topic: str = "", image: bool | None = None) -> str | None:
        """image：True 一定配圖、False 不配、None 她自己決定"""
        prov = self.s.provider()
        if prov is None:
            return None
        prompt = self._today_bits() + (f"这条说说想写的主题：{topic}\n" if topic else "") + "\n写说说："
        rule = WRITER + (IMAGE_RULE if image else IMAGE_MAYBE if image is None else "")
        resp = await asyncio.wait_for(prov.text_chat(prompt=prompt, system_prompt=self._persona() + rule), timeout=60)
        text = (getattr(resp, "completion_text", "") or "").strip()
        text = re.sub(r"(?:<<|《《|＜＜)[^\n]*", "", text)
        # 把起草稿寫成正文了：挑出終稿（「配图：」那行先拿開、挑完放回去），挑不出來這次不寫。自動模式會直接發出去，這步不能省
        pics = IMAGE_LINE_RE.findall(text)
        body = IMAGE_LINE_RE.sub("", text).strip()
        final = pick_final(body)
        if final != body:
            logger.warning(f"[lively_qzone] 说说像起草稿，{'挑出了终稿' if final else '这次不写'}:\n{text[:400]}")
            if not final:
                return None
            text = final + "".join(f"\n配图：{p}" for p in pics[-1:])
        text = re.sub(r"&&[^&]*&&", "", text).replace("。", "").strip().strip('"“”')
        return text[:600] or None

    async def _draw(self, prompt: str, with_self: bool) -> str:
        meta = self.context.get_registered_star("astrbot_plugin_lively_comic")
        comic = getattr(meta, "star_cls", None) if meta else None
        if comic is None or not hasattr(comic, "draw_bytes"):
            raise RuntimeError("画图插件没加载")
        data = await comic.draw_bytes(prompt, with_self)
        path = self.dir / f"qzone_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}.png"
        path.write_bytes(data)
        return str(path)

    def waiting(self) -> dict | None:
        """還在等主人的草稿（過了 DRAFT_TTL 的不算）"""
        d = self.state.get("draft")
        return d if d and time.time() - d.get("ts", 0) <= DRAFT_TTL else None

    def status_line(self) -> str:
        """給心跳那頁的一句：上一條說說是什麼時候發的、有沒有一條在等主人（只是事實）"""
        posted = self.state.get("posted") or []
        if posted:
            days = int((time.time() - posted[-1].get("ts", 0)) // 86400)
            out = self.s.fill(f"〈我〉上一条说说是{'今天' if days == 0 else f'{days} 天前'}发的")
        else:
            out = self.s.fill("〈我〉还没发过说说")
        d = self.waiting()
        return out + (self.s.fill(f"，有一条在等〈主人〉看：「{d['text'][:30]}」") if d else "")

    def _drawn(self, origin: str) -> dict | None:
        """她在 origin 這個聊天裡 DRAWN_TTL 以內畫的最後一張（DRAWN_FROM 裡的畫圖插件都算），{"path", "ts", "by"}"""
        best = None
        for name, fn in DRAWN_FROM:
            try:
                meta = self.context.get_registered_star(name)
                star = getattr(meta, "star_cls", None) if meta else None
                got = getattr(star, fn)(origin) if star and hasattr(star, fn) else None
            except Exception as e:
                logger.warning(f"[lively_qzone] 问 {name} 刚画的图失败: {e}")
                got = None
            if (got and got.get("path") and time.time() - got.get("ts", 0) <= DRAWN_TTL and Path(got["path"]).exists()
                    and (best is None or got["ts"] > best["ts"])):
                best = {**got, "by": name}
        return best

    async def her_draft(self, text: str, image: str = "", origin: str = "", source: str = "", by: str = "chat",
                        owner: bool = False) -> str:
        """她自己想發的說說（聊天裡的工具、心跳的動作都走這裡）：不管什麼模式一律先給主人審。
        image：空的不配；「刚画的」配她在 origin 這個聊天裡剛畫的那張；其他當成英文畫面描述新畫一張。
        owner：說話的是〈主人〉（聊天裡的工具）：一天最多 HER_DAY_MAX 條那道不看，照樣算進今天送的條數；一次只能有一條在等照擋。
        回傳給她的只說事實（送沒送、為什麼）"""
        d = self.waiting()
        if d:
            return f"已经有一条说说在等〈主人〉看：「{d['text'][:40]}」，这条没送"
        day = datetime.now(TZ).strftime("%Y-%m-%d")
        cnt = self.state.setdefault("her", {})
        if cnt.get("day") != day:
            cnt.update(day=day, n=0)
        if cnt["n"] >= HER_DAY_MAX and not owner:
            return (f"今天已经给〈主人〉送了 {cnt['n']} 条说说（〈我〉自己送去审的一天最多 {HER_DAY_MAX} 条），这条没送；"
                    "〈主人〉叫〈我〉发的不看这一道" + ASK_OWNER)
        text = re.sub(r"(?:<<|《《|＜＜)[^\n]*", "", str(text or ""))
        final = pick_final(text.strip())
        if not final:
            return "说说正文像起草稿，没挑出能发的那句，这条没送"
        text = re.sub(r"&&[^&]*&&", "", final).replace("。", "").strip().strip('"“”')[:300]
        if not text:
            return "说说正文是空的，这条没送"
        image = str(image or "").strip()
        pic = pic_prompt = None
        if image and re.fullmatch(r"(?:刚才?|最近)?画(?:好)?的(?:那张|图|那张图)?|刚才?的图|刚画", image):
            got = self._drawn(origin) if origin else None
            if not got:
                return ("看 QQ 的时候没有「刚画的」那张，" if not origin else "这个聊天里两小时内没画过图，") + "这条没送"
            dst = self.dir / f"qzone_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}_drawn{Path(got['path']).suffix or '.png'}"
            dst.write_bytes(Path(got["path"]).read_bytes())
            pic = str(dst)
        elif image:
            with_self = "[me]" in image.lower()
            pic_prompt = re.sub(r"\[me\]", "", image, flags=re.I).strip()
            try:
                pic = await self._draw(pic_prompt, with_self)
            except Exception as e:
                return f"配图没画出来（{str(e)[:60]}），这条没送"
        cnt["n"] += 1
        self.state["draft"] = {"text": text, "ts": int(time.time()), "topic": "", "image": pic, "image_prompt": pic_prompt,
                               "want_image": None, "via": by, "origin": origin, "source": source}
        self._save()
        self._log("draft", text=text, image=_pic_name(pic), image_prompt=pic_prompt, via=by, source=source)
        self._ask_owner(text, pic, source)
        return self.s.fill("草稿" + ("连图" if pic else "") + "给〈主人〉看了，〈主人〉回了才会发")

    def settled(self, draft: dict | None, how: str, text: str = ""):
        """主人回了她自己送的草稿（私信或面板）：把結果記進她發起的那個聊天，下一輪她就知道。定時寫的不用"""
        if not draft or draft.get("via") not in ("chat", "heartbeat"):
            return
        umo = draft.get("origin") or self.s.private_umo(self.s.owner())
        old = draft.get("text", "")[:40]
        msg = {
            "posted": f"〈主人〉看过了，说说「{old}」发出去了" + ("（带着那张图）" if draft.get("image") else ""),
            "edited": f"〈主人〉把说说「{old}」改成「{text[:60]}」发出去了",
            "rejected": f"〈主人〉没让发那条说说：「{old}」",
            "rewrite": f"〈主人〉说那条说说「{old}」要重写，没发",
        }.get(how)
        if msg:
            note_outbox(umo, self.s.fill(msg), "说说")

    async def new_draft(self, topic: str = "", image: bool | None = None, review: bool = False) -> str:
        """image：True 一定配圖、False 不配、None 她自己決定（畫好一起發）；review：不管是不是自動模式都先給主人審（配圖的一定先審）"""
        d = self.waiting()
        if d:  # 有一條在等主人（多半是她自己送的）：不蓋掉
            return f"已经有一条说说在等〈主人〉看：「{d['text'][:30]}」"
        text = await self.write(topic, image)
        if not text:
            return "没写出来，下次再说"
        m = IMAGE_LINE_RE.search(text) if image is not False else None
        text = IMAGE_LINE_RE.sub("", text).strip()[:300]
        if not text:
            return "没写出来，下次再说"
        if image and not m:
            return "没想好配什么图，下次再说"
        pic = pic_prompt = None
        if m:
            pic_prompt = m.group(1).strip()
            with_self = "[me]" in pic_prompt.lower()
            pic_prompt = re.sub(r"\[me\]", "", pic_prompt, flags=re.I).strip()
            try:
                pic = await self._draw(pic_prompt, with_self)
            except Exception as e:
                logger.warning(f"[lively_qzone] 配图没画出来: {e}")
                if image:
                    return f"配图没画出来：{e}"
                pic_prompt = None  # 她自己想配的，畫不出來就只發字
        if self.state["mode"] == "auto" and not (review or pic):
            tid = await self.publish(text)
            self._notify(f"〈我〉写的说说已经发到 QQ 空间了（自动模式，不用〈主人〉审）：「{text}」", "说说自动发了")
            return f"已发布（{tid}）"
        self.state["draft"] = {"text": text, "ts": int(time.time()), "topic": topic, "image": pic, "image_prompt": pic_prompt, "want_image": image}
        self._save()
        self._log("draft", text=text, image=_pic_name(pic), image_prompt=pic_prompt)
        self._ask_owner(text, pic)
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

    # ---------- 她自己發 ----------
    @filter.llm_tool("lively_qzone_post")
    async def post_tool(self, event: AstrMessageEvent, text: str, image: str = ""):
        """在你的 QQ 空间发一条说说（QQ 好友都看得到）。会先私信给主人看，主人同意了才发出去，主人怎么回的之后你会知道。一次只能有一条在等主人，一天最多送 3 条（主人叫你发的不受这个限制）。

        Args:
            text(string): 说说正文
            image(string): 不配图就不写；写「刚画的」配你在这个聊天里两小时内画的最后一张；写别的就当成英文画面描述，新画一张（画面里有你自己就在最后加 [me]）
        """
        umo = event.unified_msg_origin
        qq = str(event.get_sender_id() or "")
        who = "〈主人〉" if self.s.is_owner(qq) else (event.get_sender_name() or qq)
        where = "〈主人〉的私聊" if not event.get_group_id() and self.s.is_owner(qq) else (
            f"群 {event.get_group_id()}" if event.get_group_id() else f"和 {who} 的私聊")
        try:
            meta = self.context.get_registered_star("astrbot_plugin_lively_mind")
            mind = getattr(meta, "star_cls", None) if meta else None
            if event.get_group_id() and mind is not None and hasattr(mind, "where"):
                where = mind.where(umo) or where  # 群名；私聊照上面寫「和誰的私聊」
        except Exception:
            pass
        said = (event.message_str or "").strip()[:40]
        source = f"在{where}，{who}说了「{said}」之后〈我〉想发的" if said else f"在{where}〈我〉想发的"
        try:
            return self.s.fill(await self.her_draft(text, image, origin=umo, source=self.s.fill(source), by="chat",
                                                owner=self.s.is_owner(qq)))
        except Exception as e:
            logger.error(f"[lively_qzone] 她要发的说说没送成: {e}", exc_info=True)
            return f"没送成：{str(e)[:80]}"

    # ---------- 主人看草稿：主人怎麼回都交給她，她用這個工具去辦（10-26 用戶定不做固定指令） ----------
    async def review(self, action: str, text: str = "") -> str:
        """處理在等主人的那條草稿：发 / 不发 / 改（用 text 發）/ 重写（丟掉，她再送一條）。回傳只講事實"""
        d = self.waiting()
        if not d:
            return "现在没有在等〈主人〉看的说说"
        a = str(action or "").strip().translate(T2S)
        text = re.sub(r"&&[^&]*&&", "", str(text or "")).strip()[:300]
        old = d["text"][:40]
        if any(w in a for w in ("不发", "别发", "丢", "算了", "不要")):
            self.state["draft"] = None
            self._log("rejected", text=d["text"], image=_pic_name(d.get("image")), via="chat")
            self.settled(d, "rejected")
            self._save()
            return f"那条说说「{old}」不发了，丢掉了"
        if any(w in a for w in ("重写", "换一条", "重新写")):
            self.state["draft"] = None
            self._log("rejected", text=d["text"], image=_pic_name(d.get("image")), rewrite=True, via="chat")
            if d.get("via") in ("chat", "heartbeat"):
                self.settled(d, "rewrite")
                her = self.state.setdefault("her", {})
                her["n"] = max(0, int(her.get("n") or 0) - 1)  # 主人要重寫的那條不算進今天送的條數
            self._save()
            return f"那条说说「{old}」丢掉了没发；要重写就用 lively_qzone_post 再送一条"
        edit = "改" in a or (text and text != d["text"])
        if edit and not text:
            return "没改：要改的话把改好的正文写在 text 里"
        if not edit and "发" not in a and "好" not in a and "可以" not in a:
            return "没做：action 写发、不发、改或重写"
        body = text if edit else d["text"]
        try:
            tid = await self.publish(body, d.get("image"))
        except Exception as e:
            logger.error(f"[lively_qzone] 发说说失败: {e}")
            return f"没发出去：{str(e)[:80]}（草稿还在等）"
        self.state["draft"] = None
        if edit:
            self._log("edited", before=d["text"], after=body, via="chat")
        self.settled(d, "edited" if edit else "posted", body)
        self._save()
        logger.info(f"[lively_qzone] 主人在聊天里让发，已发布 {tid}")
        return ("改成「" + body[:60] + "」发出去了" if edit else f"说说「{old}」发出去了") + ("（带着那张图）" if d.get("image") else "")

    @filter.on_llm_request(priority=100)
    async def hide_review(self, event: AstrMessageEvent, req):
        """只有主人那邊看得到處理草稿的工具"""
        tools = getattr(req, "func_tool", None)
        if not tools or self.s.is_owner(str(event.get_sender_id() or "")):
            return
        try:
            if tools.get_tool("lively_qzone_review"):
                tools.remove_tool("lively_qzone_review")
        except Exception:
            pass

    @filter.llm_tool("lively_qzone_review")
    async def review_tool(self, event: AstrMessageEvent, action: str, text: str = ""):
        """处理在等主人看的那条 QQ 空间说说：发、不发、改（用 text 里改好的正文发）、重写（这条丢掉不发，之后你可以用 lively_qzone_post 再送一条）。
        只有主人说了才做得成；主人怎么回没有固定说法，你自己理解。

        Args:
            action(string): 发、不发、改、重写
            text(string): 改的时候，改好的正文（主人给了版本就用主人的）；其他时候不写
        """
        if not self.s.is_owner(str(event.get_sender_id() or "")):
            return self.s.fill("没做：说说发不发要〈主人〉说了才行")
        try:
            return self.s.fill(await self.review(action, text))
        except Exception as e:
            logger.error(f"[lively_qzone] 处理说说草稿失败: {e}", exc_info=True)
            return f"没做成：{str(e)[:80]}"

    # ---------- 後台指令（私信，回覆是【后台】白話） ----------
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=1450)
    async def owner(self, event: AstrMessageEvent):
        if not self.s.is_owner(event.get_sender_id()):
            return
        text = (event.message_str or "").strip().translate(T2S)
        draft = self.waiting()
        reply = None
        try:
            if text == "删说说":
                if not self.state["posted"]:
                    reply = "还没发过说说"
                else:
                    last = self.state["posted"][-1]
                    await self.delete(last["tid"])  # 刪成功了才從記錄裡拿掉，失敗還能再刪
                    self.state["posted"].pop()
                    self._save()
                    reply = f"删掉了最近一条说说：{last['text'][:30]}"
            elif text.startswith("说说") and text[2:].strip() in ("自动", "审核", "开", "开启", "关", "关闭", "", "状态"):
                arg = text[2:].strip()
                if arg == "自动":
                    self.state["mode"] = "auto"
                    reply = "说说改成自动：定时写的写好直接发（她自己送的、配了图的照样先给你看）"
                elif arg == "审核":
                    self.state["mode"] = "review"
                    reply = "说说改成审核：写好先给你看再发"
                elif arg in ("开", "开启"):
                    self.state["enabled"] = True
                    reply = f"定时写说说开了（每天 {DRAFT_WINDOW[0]}～{DRAFT_WINDOW[1]} 之间，大约隔天一条）"
                elif arg in ("关", "关闭"):
                    self.state["enabled"] = False
                    reply = "定时写说说关了（她自己用工具送、面板「写一条」不受影响）"
                else:
                    try:
                        ok = await self.check()
                    except Exception as e:
                        ok = f"空间连不上：{e}"
                    reply = (
                        f"说说：定时写{'开' if self.state['enabled'] else '关'}，"
                        f"{'直接发' if self.state['mode'] == 'auto' else '先审核'}；"
                        f"发过 {len(self.state['posted'])} 条\n{ok}"
                        + (f"\n有一条草稿在等你：{draft['text']}" if draft else "")
                    )
                self._save()
        except Exception as e:
            reply = f"出错了：{e}"
            logger.error(f"[lively_qzone] 处理主人指令失败: {e}")
        if reply is not None:
            event.stop_event()
            await event.send(MessageChain().message(self.s.admin_text(reply)))
