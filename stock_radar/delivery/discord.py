"""Discord 推播。設定 Bot token 與頻道時用 Bot 發送（可附按鈕），否則用 Webhook，都沒有就只寫入 outbox。"""
import logging

from ..config import ROOT, secret
from ..http import request
from ..timeutil import iso

log = logging.getLogger(__name__)
API = "https://discord.com/api/v10"
LIMIT = 1900
# Discord 會以錯誤碼 40333 拒絕瀏覽器格式的 User-Agent，必須用 DiscordBot 格式
UA = {"User-Agent": "DiscordBot (https://github.com/stock-radar, 0.1)"}


def split(text: str, limit: int = LIMIT) -> list[str]:
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            chunks.append(cur + line[: limit - len(cur)])
            line, cur = line[limit - len(cur):], ""
        if len(cur) + len(line) > limit:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        chunks.append(cur)
    return chunks


def _components(buttons: list[tuple[str, str, int]] | None) -> list[dict]:
    if not buttons:
        return []
    rows = [buttons[i:i + 5] for i in range(0, min(len(buttons), 25), 5)]
    return [{"type": 1, "components": [{"type": 2, "style": style, "label": label, "custom_id": cid}
                                       for label, cid, style in row]} for row in rows]


def _outbox(text: str) -> None:
    path = ROOT / "data" / "outbox.log"
    path.parent.mkdir(exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n===== {iso()} =====\n{text}\n")


def send(text: str, buttons: list[tuple[str, str, int]] | None = None) -> bool:
    """buttons：[(標籤, custom_id, 樣式)]，樣式 1=藍 2=灰 3=綠 4=紅。只有 Bot 模式能顯示按鈕。"""
    _outbox(text)
    token, channel, webhook = secret("DISCORD_BOT_TOKEN"), secret("DISCORD_CHANNEL_ID"), secret("DISCORD_WEBHOOK_URL")
    chunks = split(text)
    try:
        for i, chunk in enumerate(chunks):
            payload = {"content": chunk, "allowed_mentions": {"parse": []}}
            if i == len(chunks) - 1 and buttons:
                payload["components"] = _components(buttons)
            if token and channel:
                request("POST", f"{API}/channels/{channel}/messages", json=payload,
                        headers={"Authorization": f"Bot {token}", **UA})
            elif webhook:
                payload.pop("components", None)
                request("POST", webhook + "?wait=true", json=payload, headers=UA)
            else:
                return False
        return True
    except Exception as e:
        log.error("Discord 發送失敗：%s", e)
        return False


def configured() -> bool:
    return bool((secret("DISCORD_BOT_TOKEN") and secret("DISCORD_CHANNEL_ID")) or secret("DISCORD_WEBHOOK_URL"))
