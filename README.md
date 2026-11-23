# 译词

本地单词应用：DeepSeek 生成例句，你翻译，AI 给两个分数，按配置规则判过关，
用精简版 FSRS 排复习。Flask + SQLite + dataset，单机单用户。

## 安装

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
copy config\ai.example.json config\ai.json    # 然后填 api_key
```

> 也可以跳过最后一步：启动后在 **我的 → 设置 → API Key** 里直接粘贴，会写回 `config/ai.json`。

## 导入词书

**界面导入（推荐）**：仪表盘 →「＋ 新建词书（粘贴 JSON）」，填词书名，把这样的 JSON 粘进去，
点确认。服务端会先校验格式，不合法会直接把错误显示在弹窗里，让你改。

```json
{"data":[{"word":"I","translation":"我"},{"word":"one","translation":"一"}]}
```

**命令行导入**：

```bash
python tools/import_words.py --source "D:\path\cet4.db" --name cet4
python tools/import_words.py --source words.csv --name mywords     # CSV 需 word/translation 两列
```

旧库只取 `word` 与 `translation`，其余字段丢弃，全部重置为"未学习"。

## 启动

```bash
python run.py                 # http://127.0.0.1:5000
python run.py --port 5050     # 临时换端口
python run.py --allow-lan     # 临时允许局域网访问（手机/别的电脑）
```

端口和"是否允许非本机访问"也可以在 **我的 → 设置 → 服务器** 里改，改完重启生效。
默认只接受本机请求；打开开关后启动时会打印局域网地址，手机等设备首次打开要输入
设置页显示的 **6 位数字令牌**，验证一次后本次浏览器会话内不用再输。

## 自检

```bash
python -m unittest discover -s tests -t .   # 65 个测试，不联网
python tools/check_ai.py                    # 真实调一次 AI，验证 key 与参数
```

## 目录

```
config/   app.json 业务参数 / fsrs.json 算法参数 / ai.json 密钥 / prompts.json 提示词
data/     library.db 词库 / wordbooks.db 词本 / tasks.db 任务 / user/history.db 历史
app/      Flask 应用（views / services / db / templates / static）
tools/    导入词书、AI 自检
docs/     DESIGN.md 设计文档 / icon/ 应用图标的生成源
build_exe.py  打包成 exe 的脚本
```

## 图标

`docs/icon/` 里是图标生成源：角色原图、`render.html`（HTML+CSS 版式）、成品 `preview.png`。
改完重新渲染并缩尺寸，流程写在 `docs/icon/README.md`。

## 打包成 exe

```bash
pip install pyinstaller
python build_exe.py
```

产物是 `dist/译词/`：里面 `译词.exe`、`_internal/` 运行库目录，以及一份 `译词.ico`，
图标就是上面这套。发给别人时把整个文件夹压成 zip。

启动后会自动把访问地址写进剪贴板，并打开默认浏览器（源码运行时默认不打开，
`python run.py --browser on` 可以强制打开）。

**快捷方式图标空白**：exe 的图标资源是好的（`ExtractAssociatedIcon` 和 shell 都能读到），
空白通常是 Windows 图标缓存或快捷方式指向了旧路径。处理办法：删掉旧快捷方式 →
运行 `ie4uinit.exe -show` 清缓存 → 重新创建。还不行就在快捷方式上右键「属性 → 更改图标」，
选包里的 `译词.ico`。

为什么不用单文件（`--onefile`）：单文件每次启动都要把运行库解包到临时目录，
部分机器上会被安全软件或权限策略拦掉（本机就复现了 `VCRUNTIME140.dll: Permission denied`），
而且启动会慢一拍。onedir 直接就地加载，双击即用。

exe 里只有程序本体（代码 + 模板 + 静态资源 + 出厂提示词），**不含**词库、配置和个人数据；
第一次运行会在 exe 旁边自动生成 `config/` 和 `data/`。要带词库就把 `data/library.db`
放进 exe 同目录的 `data/` 下。

## 几条约定

- **AI 只打分，不过关判定在本地**：改阈值只改 `config/app.json`，不动提示词（提示词是缓存前缀）。
- **API Key 与提示词都能在界面里改**：我的 → 设置；提示词有"恢复默认"。
- **AI 出错不算你答错**：评分失败时 `task_state` 与 `error_count` 都不动。
- **余额不足会暂停**：一轮做完一部分就停，剩下的留在任务库，充值后从首页"继续做题"接上。
- **一轮要连续答对两次才算过**（首次答错后 -1 → 0 → 1），进度按"已完成/总数"显示。
- **拼写训练不参与复习计划**，只进历史统计；拼到一半关掉/重启，残留队列会被自动清掉。
- **设置页最底部有"初始化"**：清空全部学习数据，词书保留，适合把项目交给别人。

## 交给别人之前

1. 设置页 → 初始化 → 勾选"同时清空 API Key" → 执行
2. 或者手动删掉 `config/ai.json`、`data/tasks.db`、`data/user/history.db`、`data/memory/`
3. `data/library.db` 可以留着，也可以删掉让对方自己导入词书
