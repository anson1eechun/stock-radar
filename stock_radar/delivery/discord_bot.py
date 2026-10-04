"""Discord 按鈕 Bot：處理建議訊息上的「已進場」「略過」按鈕，寫入 user_actions。"""
import logging

import discord

from ..config import secret
from ..db import connect
from ..timeutil import iso

log = logging.getLogger(__name__)


def record(rec_id: int, action: str, price: float | None, user: str) -> None:
    conn = connect()
    conn.execute("INSERT INTO user_actions (rec_id, action, price, at, note) VALUES (?,?,?,?,?)",
                 (rec_id, action, price, iso(), f"discord:{user}"))
    conn.commit()
    conn.close()


class TookModal(discord.ui.Modal):
    def __init__(self, rec: dict):
        super().__init__(title=f"#{rec['id']} {rec['symbol']} {rec['name']} 進場回報")
        self.rec = rec
        self.price = discord.ui.TextInput(label="成交價", default=f"{rec['entry_high']:g}", max_length=12)
        self.add_item(self.price)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = float(self.price.value)
        except ValueError:
            await interaction.response.send_message("成交價格式錯誤", ephemeral=True)
            return
        record(self.rec["id"], "took", price, str(interaction.user))
        await interaction.response.send_message(f"已記錄 #{self.rec['id']} {self.rec['symbol']} 進場 {price:g}", ephemeral=True)


class Bot(discord.Client):
    async def on_ready(self):
        log.info("Discord Bot 已連線：%s", self.user)

    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        action, _, rec_id = (interaction.data or {}).get("custom_id", "").partition(":")
        if action == "noop":
            await interaction.response.send_message("✅ 按鈕連線正常", ephemeral=True)
            return
        if action not in ("took", "skip") or not rec_id.isdigit():
            return
        conn = connect()
        rec = conn.execute("SELECT id, symbol, name, entry_high FROM recommendations WHERE id=?", (int(rec_id),)).fetchone()
        conn.close()
        if not rec:
            await interaction.response.send_message(f"找不到建議 #{rec_id}", ephemeral=True)
        elif action == "took":
            await interaction.response.send_modal(TookModal(dict(rec)))
        else:
            record(rec["id"], "skip", None, str(interaction.user))
            await interaction.response.send_message(f"已記錄略過 #{rec['id']} {rec['symbol']}", ephemeral=True)


def run() -> None:
    token = secret("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("未設定 DISCORD_BOT_TOKEN")
    Bot(intents=discord.Intents.default()).run(token, log_handler=None)
