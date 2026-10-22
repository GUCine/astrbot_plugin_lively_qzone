# QQ 空間說說

> 寫 QQ 空間說說，主人審核後發佈

AstrBot 插件，屬於「擬真群友」（lively）插件套件。適用 AstrBot >=4.29.0 + NapCat（aiocqhttp）。

主人、群、機器人名字、人設用詞這些共用設定在 [astrbot_plugin_lively_gate](https://github.com/GUCine/astrbot_plugin_lively_gate) 裡填，建議一起裝。

## 功能

```text
機器人的 QQ 空間：寫說說 → 私信主人審核 → 通過才發。

- 隔一兩天的晚上，按今天群裡發生的事和機器人的心情寫一條草稿，私信主人
- 主人回：發 / 不發 / 重寫 / 改 新內容（用主人的版本發）
- 「說說 自動」後不再審核直接發（發完照樣告訴主人）；「說說 審核」改回來
- 其他指令：寫說說 [主題]、刪說說（刪最近一條機器人發的）、說說 開 / 關 / 狀態
- 發說說的介面參考 Zhalslar/astrbot_plugin_qzone（MIT）：用 NapCat 的 get_cookies 拿空間 Cookie
```

## 依賴

NapCat（用 get_cookies 拿 QQ 空間 Cookie）。發說說的介面參考了 [Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone)（MIT）。

## 配置

在 AstrBot WebUI 的插件配置裡填，或者直接編輯 `data/config/astrbot_plugin_lively_qzone_config.json`。留空的公共項（主人、群、機器人名字、平台）會用 astrbot_plugin_lively_gate 裡填的。

| 配置項 | 說明 | 預設 |
| --- | --- | --- |
| `provider_id` | 寫說說用的模型：留空用 AstrBot 預設模型 | （空） |
| `persona_id` | 機器人用的人格 ID：寫東西時套用這個人格的提示詞，口吻和聊天一致。留空用 AstrBot 預設人格 | （空） |
| `owner_qq` | 主人 QQ（留空用門禁插件裡填的） | （空） |

## 資料

執行資料在 `data/plugin_data/astrbot_plugin_lively_qzone/`。

## 許可

MIT，見 [LICENSE](LICENSE)。
