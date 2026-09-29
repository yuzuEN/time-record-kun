# ⏱️ Discord 語音頻道時間記錄機器人

自動記錄成員進出語音頻道的時間戳記，並依頻道類別（📚 讀書、☕ 休息、💼 工作、⛏️ Minecraft）統計每天各待了多久。所有統計都能在文字頻道用斜線指令（`/`）查詢。

---

## ✨ 功能

- **自動記錄進出時間**：加入、離開、切換語音頻道都會被記下來，不需要手動打卡。
- **頻道分類**：管理員可以把任何語音頻道指定為「讀書 / 休息 / 工作 / Minecraft」其中一類，同一類可以有多個頻道。
- **每日統計**：今天在各類頻道分別待了多久。
- **多日統計**：最近 N 天每天的明細，以及總計和日平均。
- **進出紀錄**：查看某一天每一次進出的時間點。
- **排行榜**：伺服器成員在某類頻道的時數排名。
- **CSV 匯出**：匯出原始紀錄，可以用 Excel 或 Google 試算表做進一步分析。
- **跨日自動切割**：從 23:00 讀到隔天 01:30，會拆成前一天 1 小時、隔天 1.5 小時。
- **停機補償**：機器人重新啟動時會自動校正離線期間的進出狀態。

---

## 🧠 原理

### 1. 監聽語音狀態事件

Discord 在成員的語音狀態改變時，會透過 Gateway 送出 `VOICE_STATE_UPDATE` 事件。機器人在 `on_voice_state_update` 裡比較變化前後的頻道：

| 前 (`before.channel`) | 後 (`after.channel`) | 意義 | 機器人動作 |
|---|---|---|---|
| 無 | A | 加入 A | 若 A 有被追蹤，開始一段新時段 |
| A | 無 | 離開 A | 結束目前的時段 |
| A | B | 從 A 切換到 B | 結束 A 的時段；若 B 有被追蹤，開始新時段 |
| A | A | 靜音、拒聽、開鏡頭等 | 忽略 |

### 2. 資料儲存（SQLite）

資料存在單一個 SQLite 檔案（預設 `timerecord.db`），共有三張表：

- **`channel_map`**：記錄每個伺服器中「哪個語音頻道屬於哪一類」。
- **`sessions`**：每一次待在追蹤頻道的時段，內容是 `user_id`、`channel_id`、`category`、`join_ts`（進入時間）、`leave_ts`（離開時間，還沒離開則為 `NULL`）。時間一律存成 Unix timestamp（UTC 秒數）。
- **`meta`**：存放機器人「最後存活時間」等內部資訊。

類別是在**進入頻道當下**寫進時段的，所以之後就算重新設定頻道類別，也不會改到過去的歷史紀錄。

### 3. 統計計算

查詢時，先撈出與查詢區間有重疊的時段，把每段時間**依設定時區的午夜切開**，再分別加總到各日期、各類別。尚未結束的時段（人還在頻道裡）會以「現在」當作暫時的結束時間，所以統計是即時的。

### 4. 停機補償

機器人每 60 秒寫一次「最後存活時間」（心跳）。重新啟動時：

1. 找出所有尚未結束的時段，如果那個人**已經不在該頻道**，就用上次的最後存活時間當作離開時間（誤差最多 1 分鐘）。
2. 如果有人**現在已經在追蹤頻道裡**但沒有對應的時段，就從現在開始記錄。

> ⚠️ 機器人離線期間無法得知確切的進出時間，所以停機越久，這段期間的紀錄就越不精確。建議讓機器人 24 小時運作。

---

## 🚀 安裝與執行

### 需求

- Python 3.9 以上
- 一個 Discord Bot（建立方式見下一節）

### 步驟

```bash
# 1. 安裝相依套件
pip install -r requirements.txt

# 2. 建立設定檔
copy .env.example .env      # Windows
# cp .env.example .env      # macOS / Linux

# 3. 編輯 .env，至少填入 DISCORD_TOKEN

# 4. 啟動
python bot.py
```

### `.env` 設定

| 變數 | 必填 | 說明 |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | 機器人的 Token |
| `TIMEZONE` | | 判斷「一天」的時區，預設 `Asia/Taipei` |
| `GUILD_ID` | | 填入伺服器 ID 後，斜線指令會**立即**同步到該伺服器（適合測試）。留空則全域同步 |
| `DB_PATH` | | 資料庫檔案路徑，預設 `timerecord.db` |

> 🔒 `.env` 與 `*.db` 已寫在 `.gitignore` 裡。**請絕對不要把 Token 上傳或分享給別人**，如果外洩，請立刻到 Developer Portal 重設。

---

## 🤖 建立機器人與邀請方法

### 1. 建立 Application 與 Bot

1. 前往 [Discord Developer Portal](https://discord.com/developers/applications)，點 **New Application**，取個名字。
2. 左側選 **Bot**：
   - 點 **Reset Token** 取得 Token，貼到 `.env` 的 `DISCORD_TOKEN`。
   - **Privileged Gateway Intents 都不需要開啟**（本機器人只用到預設的 `Guilds` 與 `Guild Voice States`）。
3. 左側選 **General Information**，複製 **Application ID**（也就是 Client ID）。

### 2. 產生邀請連結

**方法 A：直接修改網址**

把下面的 `YOUR_CLIENT_ID` 換成你的 Application ID，用瀏覽器開啟：

```
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot+applications.commands&permissions=52224
```

**方法 B：用 Developer Portal 產生**

左側選 **OAuth2 → URL Generator**：

- **Scopes**：勾選 `bot`、`applications.commands`
- **Bot Permissions**：勾選下表的權限

| 權限 | 用途 |
|---|---|
| View Channels | 看得到語音頻道，才能收到成員進出的事件 |
| Send Messages | 回覆指令 |
| Embed Links | 以嵌入卡片顯示統計 |
| Attach Files | `/export` 傳送 CSV 檔 |

（以上加總的權限值即為 `52224`。）

### 3. 邀請進伺服器

開啟連結 → 選擇伺服器 → 授權。你必須在該伺服器擁有「管理伺服器」權限才能邀請。

> 如果追蹤的語音頻道是私人頻道，記得在頻道權限裡讓機器人可以「檢視頻道」。

---

## 📖 使用方法

### 第一步：設定要追蹤的頻道（需要「管理伺服器」權限）

```
/setup channel:#讀書室 category:📚 讀書
/setup channel:#休息區 category:☕ 休息
/setup channel:#工作室 category:💼 工作
/setup channel:#Minecraft category:⛏️ Minecraft
```

設定完成後，之後有人進出這些頻道就會自動記錄。

### 指令一覽

| 指令 | 說明 | 範例 |
|---|---|---|
| `/setup channel category` | 把語音頻道設為某一類（管理員） | `/setup channel:#讀書室 category:📚 讀書` |
| `/unset channel` | 停止追蹤某頻道（管理員） | `/unset channel:#讀書室` |
| `/channels` | 列出目前追蹤中的頻道 | `/channels` |
| `/today [member]` | 今天各類別各待多久 | `/today` |
| `/stats [days] [member]` | 最近 N 天每天的明細、總計與日平均（預設 7 天，最多 31 天） | `/stats days:14` |
| `/log [date] [member]` | 某天每次進出的時間點（預設今天） | `/log date:2026-09-28` |
| `/status [member]` | 目前在哪個頻道、已經待了多久 | `/status` |
| `/leaderboard [category] [days]` | 伺服器排行榜（預設讀書、7 天） | `/leaderboard category:⛏️ Minecraft days:30` |
| `/export [days] [member]` | 匯出 CSV 原始紀錄（只有自己看得到） | `/export days:90` |

`[member]` 可省略，省略時查詢的是自己。

### 輸出範例

`/stats days:3`

```
📊 最近 3 天統計
09/27(日) 📚 3h20m15s　☕ 40m05s　⛏️ 2h05m30s
09/28(一) 📚 4h10m40s　☕ 55m10s　💼 1h30m00s
09/29(二) 📚 1h45m20s

📚 讀書 9h16m15s（日均 3h05m25s）  ☕ 休息 1h35m15s  💼 工作 1h30m00s  ⛏️ Minecraft 2h05m30s
⏱️ 合計 14h27m00s
```

`/log`

```
🕒 09/29(二) 進出紀錄
📚 #讀書室　08:12 → 10:03　(1h51m12s)
☕ #休息區　10:03 → 10:20　(17m05s)
📚 #讀書室　10:20 → 進行中　(45m38s)
```

---

## 📁 專案結構

```
discordTimeRecord/
├── bot.py            # 機器人主程式：事件監聽、斜線指令
├── db.py             # SQLite 資料存取
├── stats.py          # 跨日切割與統計計算
├── requirements.txt  # 相依套件
├── .env.example      # 設定檔範本
└── README.md
```

---

## ❓ 常見問題

**Q：輸入 `/` 後看不到指令？**
全域指令第一次同步可能需要一些時間才會出現。測試時可以在 `.env` 填入 `GUILD_ID`，指令就會立即出現在該伺服器。也可以試著重新開啟 Discord 客戶端。

**Q：電腦關機後資料會消失嗎？**
不會。資料都存在 `timerecord.db` 裡。不過機器人沒有執行的期間無法記錄進出。如果希望 24 小時記錄，可以把機器人放在雲端主機、樹莓派或 NAS 上執行。

**Q：可以新增其他類別嗎？**
可以。修改 `bot.py` 裡的 `CATEGORIES` 字典，例如加入 `"exercise": ("🏃", "運動")`，重新啟動後 `/setup` 就會出現新的選項。

**Q：想換時區，或把一天的分界改成凌晨 4 點？**
時區可以透過 `.env` 的 `TIMEZONE` 設定。一天的分界目前固定是午夜 00:00。
