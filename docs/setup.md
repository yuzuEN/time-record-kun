# 🛠️ 架設教學

這份文件說明如何從零開始架設自己的語音頻道時間記錄機器人：建立 Discord Bot、邀請進伺服器、在本機或雲端執行。

- [需求](#需求)
- [1. 建立 Application 與 Bot](#1-建立-application-與-bot)
- [2. 邀請機器人進伺服器](#2-邀請機器人進伺服器)
- [3. 在本機執行](#3-在本機執行)
- [4. 部署到 Railway（24 小時運作）](#4-部署到-railway24-小時運作)
- [5. 設定管理員權限](#5-設定管理員權限)
- [6. 執行測試](#6-執行測試)

---

## 需求

- Python 3.9 以上
- 一個 Discord 帳號，並且在要使用的伺服器擁有「管理伺服器」權限

---

## 1. 建立 Application 與 Bot

1. 前往 [Discord Developer Portal](https://discord.com/developers/applications)，點 **New Application**，取個名字。
2. 左側選 **Bot**：
   - 點 **Reset Token** 取得 Token，之後要填到 `.env` 的 `DISCORD_TOKEN`。
   - **Privileged Gateway Intents 都不需要開啟**（本機器人只用到預設的 `Guilds` 與 `Guild Voice States`）。
3. 左側選 **General Information**，複製 **Application ID**（也就是 Client ID）。

> 🔒 **請絕對不要把 Token 上傳或分享給別人。** 拿到 Token 的人可以完全控制你的機器人。如果外洩，請立刻回到這一頁按 **Reset Token** 重設。

---

## 2. 邀請機器人進伺服器

### 產生邀請連結

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

### 授權

開啟連結 → 選擇伺服器 → 授權。你必須在該伺服器擁有「管理伺服器」權限才能邀請。

> 如果追蹤的語音頻道是私人頻道，記得在頻道權限裡讓機器人可以「檢視頻道」，否則收不到成員進出的事件。

---

## 3. 在本機執行

```bash
# 1. 下載程式碼
git clone https://github.com/yuzuEN/time-record-kun.git
cd time-record-kun

# 2. 安裝相依套件
pip install -r requirements.txt

# 3. 建立設定檔
copy .env.example .env      # Windows
# cp .env.example .env      # macOS / Linux

# 4. 編輯 .env，至少填入 DISCORD_TOKEN

# 5. 啟動
python bot.py
```

看到 `已登入為 ...` 和 `啟動校正完成` 就代表成功了。接著到 Discord 輸入 `/setup` 設定要追蹤的頻道（見 [README 的使用方法](../README.md#-使用方法)）。

### `.env` 設定

| 變數 | 必填 | 說明 |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | 機器人的 Token |
| `TIMEZONE` | | 判斷「一天」的時區，預設 `Asia/Taipei` |
| `GUILD_ID` | | 填入伺服器 ID 後，斜線指令會**立即**同步到該伺服器（適合測試）。留空則全域同步，第一次可能要等一段時間才會出現 |
| `DB_PATH` | | 資料庫檔案路徑，預設 `timerecord.db` |

`.env` 與 `*.db` 已寫在 `.gitignore` 裡，不會被 commit。

> 取得伺服器 ID：Discord 的 **使用者設定 → 進階 → 開發者模式** 打開後，對伺服器圖示按右鍵 → **複製伺服器 ID**。

---

## 4. 部署到 Railway（24 小時運作）

機器人關掉的期間無法記錄進出，所以建議放在雲端主機上 24 小時運作。專案已附上 `railway.json`，Railway 會自動用 `python bot.py` 啟動，並在當機時自動重啟。

1. 把專案 fork 或 push 到自己的 GitHub。
2. 到 [Railway](https://railway.com/) 建立新專案，選 **Deploy from GitHub repo**，選擇這個 repo。
3. 在服務的 **Variables** 頁面加入 `DISCORD_TOKEN`（以及需要的 `TIMEZONE` 等變數）。
4. **加入 Volume 保存資料庫**：在服務上新增一個 Volume，例如掛載到 `/data`，並把變數 `DB_PATH` 設為 `/data/timerecord.db`。

> ⚠️ 第 4 步很重要。容器內的檔案在每次重新部署時都會被清掉，沒有掛 Volume 的話，**每次更新程式都會失去所有紀錄**。

其他平台（VPS、樹莓派、NAS）也可以，只要能長時間執行 `python bot.py` 即可。

---

## 5. 設定管理員權限

`/setup`、`/unset`、`/delete` 預設只有擁有「管理伺服器」權限的成員看得到、能使用。

如果想讓特定身分組（例如「機器人管理員」）也能使用這些指令，不需要改程式碼，Discord 內建就能設定：

1. **伺服器設定 → 整合（Integrations）**，選擇這個機器人。
2. 點要調整的指令（例如 `/delete`）。
3. 新增身分組或成員並允許使用。

> `/delete` 刪除的紀錄無法復原，執行前會先顯示即將刪除的時長並要求確認。請只開放給信任的人。

---

## 6. 執行測試

測試只用到 Python 內建的 `unittest`，不需要額外安裝套件：

```bash
python -m unittest discover tests
```
