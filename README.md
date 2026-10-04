# stock-radar

台股事件驅動選股系統：每天監控台股籌碼、月營收、重大訊息、新聞與美國總經事件，經過量化初篩與 AI 多空辯論後，
推播含入場區間、停損、停利的建議到 Discord，並用虛擬成交自動追蹤績效。只推播、不下單。

設計文件見 [DESIGN.md](DESIGN.md)。

## 架構

```
collectors/            context/                 screening.py        analysis.py           delivery/
證交所·櫃買·期交所  ─▶  市場狀態儀表          ─▶  量化初篩        ─▶  多方 / 空方論證   ─▶  Discord 推播
yfinance 美股·匯率      美→台供應鏈傳導           （只用 asof 以前     → 仲裁（雲端模型）     （Bot 按鈕或 Webhook）
ForexFactory·BLS·FRED   總經事件歷史反應           的資料）            levels.py 算價位       reports.py 報告
Yahoo·鉅亨 RSS ─▶ news_processing（本機模型：分類、代號、情緒、聚類）   risk.py 風控          dashboard/ Streamlit
                                                                       recommend.py 寫入    tracking.py 虛擬成交
                                                                       不可修改的建議        stats.py 歸因與淘汰
```

| 模組 | 職責 |
|---|---|
| `scheduler.py` / `jobs.py` | APScheduler 排程；台股工作用台灣時間、美股工作用美東時間。每個工作記錄在 `job_runs`，失敗會推播 |
| `screening.py` | 計算特徵、跑篩選規則、產生候選池 |
| `analysis.py` | 組裝脈絡 → 多方與空方論證 → 仲裁 → AI 組建議 |
| `levels.py` | 入場區間、停損、停利全部由規則計算，AI 不產生數字 |
| `risk.py` | 部位大小、集中度、市場狀態、事件前降曝險、處置與注意股、除權息 |
| `recommend.py` | 建議寫入後不可修改，同時建立追蹤列 |
| `tracking.py` | 用日 K 棒模擬成交、停損、停利、時間停損、論點失效出場 |
| `monitor.py` | 盤中價格到點警報、論點失效監控 |
| `scenarios.py` | 重大總經事件前產出三套劇本，公布後比對命中哪一套 |
| `stats.py` | 績效歸因、訊號淘汰、交易行為分析 |
| `llm.py` | 本機 llama.cpp（OpenAI 相容）與雲端 Anthropic 兩種客戶端 |
| `db.py` | SQLite（`data/radar.db`） |

建議分成**純規則組**與 **AI 組**分開追蹤：純規則組可以做歷史重播，AI 組只能從上線後累積。

## 安裝

```bash
cd ~/stock-radar
uv sync
cp .env.example .env      # 填入 Discord 與金鑰
uv run stock-radar init   # 回補 160 個交易日行情、美股、營收、行事曆（約 20 分鐘）
```

本機模型：`config.yaml` 的 `llm.local.base_url` 指向 llama.cpp 伺服器（預設 `http://127.0.0.1:8080/v1`）。

## `.env` 設定

| 變數 | 用途 | 沒填時 |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | 推播（頻道設定 → 整合 → Webhook） | 訊息只寫入 `data/outbox.log` |
| `DISCORD_BOT_TOKEN`、`DISCORD_CHANNEL_ID` | Bot 模式，建議訊息附「已進場／略過」按鈕 | 用 Webhook，沒有按鈕 |
| `ANTHROPIC_API_KEY` | 仲裁與情境劇本用雲端模型（官方或 OpenRouter 金鑰，網址在 `config.yaml` 的 `llm.cloud.base_url`） | 全部改用本機模型 |
| `BLS_API_KEY` | 提高 BLS 每日查詢額度（25 → 500 次） | 用免金鑰額度 |
| `FRED_API_KEY` | 事件歷史反應資料庫回補歷史首次公布值 | 只有 FOMC 升降息與系統自行累積的資料 |

Bot 模式需要另外建一個 Discord Application（不要共用其他程式的 Bot token），邀請進伺服器時給「傳送訊息」權限。

## 常用指令

```bash
uv run stock-radar status                      # 資料、工作、建議、LLM 用量
uv run stock-radar run tw_premarket --force    # 手動跑盤前分析（--skip-ai 只跑純規則組）
uv run stock-radar run tw_premarket_report --force
uv run stock-radar took 12 --price 101.5       # 回報進場；skip／exit 同理
uv run stock-radar replay --start 2026-05-04   # 純規則組歷史重播
uv run stock-radar event-study [--fred]        # 建立總經事件歷史反應資料庫
uv run stock-radar discord-test
uv run stock-radar dashboard                   # http://127.0.0.1:8501
uv run pytest
```

可執行的工作：`us_close`、`tw_premarket`、`tw_premarket_report`、`intraday_monitor`、`news_cycle`、`tw_postmarket`、
`tw_evening`、`macro_refresh`、`scenario_build`、`us_premarket_report`、`revenue_collect`、`weekly_review`。

## 常駐執行（systemd 使用者服務）

```bash
mkdir -p ~/.config/systemd/user
cp deploy/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now stock-radar-scheduler stock-radar-dashboard
systemctl --user enable --now stock-radar-bot      # 有設定 Bot token 才需要
loginctl enable-linger $USER                       # 登出後繼續執行
journalctl --user -u stock-radar-scheduler -f
```

## 每日排程（台灣時間，美國工作依美東時間自動處理夏令時間）

| 時間 | 工作 |
|---|---|
| 05:30（美東 17:30） | 美股、匯率、利率收盤資料 |
| 06:30 | 盤前分析：新聞處理、市場狀態、量化初篩、純規則組、AI 辯論 |
| 07:45 | 台股盤前報告與建議卡片 |
| 09:00–13:35 每 5 分鐘 | 價格到點警報 |
| 07:00–23:55 每 15 分鐘 | 新聞收集、本機模型處理、論點失效監控 |
| 14:45–17:45 每小時 | 盤後行情、三大法人、虛擬追蹤、盤後報告（完成一次即停止） |
| 19:30 | 未來 36 小時重大總經事件的情境劇本 |
| 21:00（美東 09:00） | 美股盤前報告 |
| 21:45 | 融資融券、注意／處置股、除權息、外資期貨、重大訊息 |
| 總經公布時 | 自動監看實際值，推播快訊並比對情境劇本 |
| 每月 1–12 日 18:00 | 月營收與亮點 |
| 週六 10:00 | 每週檢討：績效、訊號歸因與淘汰、交易行為、系統狀況 |

## 重要限制

- 只做多、只做波段與中長線，不做當沖、不下單。
- 行情未還原除權息；除權息會在風控備註提醒。
- 虛擬成交用日 K 棒模擬，同一根 K 棒同時觸及停損與目標時保守算停損。
- AI 組無法做歷史重播（模型知道未來），只能從上線後開始累積；每類訊號大約要上百筆才看得出真假。
- 供應鏈關係（`supply_chain.yaml`）是種子清單，請依自己的研究調整。
