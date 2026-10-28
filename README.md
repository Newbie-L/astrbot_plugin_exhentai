# astrbot_plugin_exhentai

监听 ExHentai / E-Hentai 画廊链接，下载后压缩成加密包发到群聊。

## 能做什么

- 私聊发一条画廊链接，例如 `https://exhentai.org/g/1234567/abcdef1234/`，自动进队列下载。
- 链接没被自动监听到时，手动发 `/ex_dl <链接>`。
- 下载完后自动打成加密 zip：密码是随机 6 位数字，压缩包名就是画廊标题（去掉文件名非法字符、截断到 120 字）。
- 压缩完后把压缩包和密码发到群聊，同一条消息里既有包又有密码。
- `/ex_status [gid]` 查进度、压缩包路径和密码，`/ex_list` 列出已完成的，`/ex_send <gid>` 在当前会话手动补发压缩包。

## 安装

1. 把整个文件夹放进 AstrBot 的插件目录，重启 AstrBot。
2. 安装依赖：`pip install -r requirements.txt`（AstrBot 环境一般自带 aiohttp，pyzipper 是加密 zip 用的，必须装；没装时插件会退到系统 `zip -P`，两者都没有就压缩失败、不会发没加密的包）。
3. 在 AstrBot 后台打开本插件的配置。

## 发到哪里？

发包目标按这个顺序定：

1. 配置里的 `target_group_umo`，例如 `aiocqhttp:GroupMessage:123456789`；
2. 没配的话就发回触发会话：群里触发发回群，私聊触发发回私聊；
3. 只有 `send_zip_to_group` 关掉时才只留本地，用 `/ex_send <gid>` 在当前会话手动补发。

v0.2.1 之前第 2 步只认群，私聊触发会 `no_target`、哪里都不发，这是 bug，v0.2.2 已修。

`unified_msg_origin` 可以在 AstrBot 日志 / 该群的消息事件里看到，格式就是 `平台:GroupMessage:群号`。

## 缺图怎么办？

单张失败重试 3 次，整遍下完后缺的序号再自动重下最多 2 轮，残文件写 `.part`、完整才重命名，不会把残文件当成功。2 轮后还缺的标 `partial`，`/ex_status` 里会列出缺图序号，并且**不会把不完整的包当成完整包发出去**。再跑一次 `/ex_dl <链接>` 会只补缺的序号（已成功的会跳过），补完重新压缩、按触发会话发送。旧版已完成但实际缺图（已下载数 < 总数）的任务，再跑一次同样会触发补下。

v0.2.3 另外修了两个会让人以为“卡住”的问题：画廊分页不再假设每页 40 张（账号设成每页 20 张时，44 张的画廊之前只会拿到前 20 张）；状态改为每张都更新（之前只每 10 张更新一次，查状态时会一直停在旧数字上）；重启插件后旧的 `downloading` 状态不会再被误判为“已在队列”而永远卡住。

v0.2.4 修了断点续传：下载中途 TLS 断掉（ContentLengthError，例如每次都卡在 273020/275256 字节，只差最后 2KB）时，旧代码会把 `.part` 删掉、下次又从 0 开始，永远在同一个位置断掉。新代码保留 `.part`，下次带 `Range` 续传最后一点，完整后才重命名。

## 配置（关键）

| 配置 | 说明 |
|---|---|
| `ipb_member_id` / `ipb_pass_hash` | 你自己登录 exhentai.org 后的浏览器 cookie |
| `igneous` | exhentai 入站 cookie，没有它只能看到 sad panda |
| `download_dir` | 留空 = 插件数据目录下的 `downloads/`，图片文件夹和压缩包都在这里 |
| `download_delay` | 每张图间隔，默认 1 秒 |
| `max_concurrent` | 同时下载的画廊数，默认 1 |
| `only_private` | 默认只在私聊自动触发，群聊链接不自动下载；手动 `/ex_dl` 不受此开关限制 |
| `target_group_umo` | 发压缩包的目标群，留空则发回触发会话（群发回群、私聊发回私聊） |
| `send_zip_to_group` | 默认开，关了就只留本地 |
| `delete_images_after_zip` | 默认关，开了压缩成功后删原图片（保留 metadata.json） |

取 cookie 的办法：用你平时能正常打开 exhentai.org 的浏览器，开发者工具 → 应用/存储 → Cookie → exhentai.org，复制上面三个值。**只填你自己的，不要发给别人，也不要提交到公开仓库。**

e-hentai.org 的公开画廊一般不需要 cookie；exhentai.org 没有 cookie 大概率直接失败，插件会在进队列时提醒你。

## 下载流程

画廊元数据（`api.php` 的 `gdata`，拿标题和总页数）→ 翻画廊分页收集图片页 → 逐个图片页解析原图地址 → 带 Referer 逐张落盘 → 文件夹里附一份 `metadata.json`。

已下载过的 gid 会直接跳过，不会重复下载；中途失败的任务状态是 `failed`，看 `/ex_status <gid>` 里的错误信息再重试。

## 注意事项

- ExHentai 有图片流量配额，刷太快会返回 509。插件默认单并发 + 1 秒间隔，509 时会等 60 秒再试。不要把并发和间隔改得太激进。
- 密码和压缩包在同一条群消息里，群成员都能打开；6 位数字密码本身也不抗暴力破解，加密只是防平台扫描 / 非群成员直接拿到文件，别当成强保密手段。
- 只下载你自己有权保存的内容，往群里发之前先确认那个群适不适合发这类内容。
- 压缩失败时图片会保留、状态是 `zip_failed`，修好依赖（装 pyzipper）后重新 `/ex_dl` 即可重试，不会发没加密的包。
- AstrBot 版本差异：如果你的版本没有 `@filter.regex`，把 `main.py` 里 `on_gallery_link` 上面的装饰器换成 `@filter.event_message_type(EventMessageType.ALL)` 即可，函数体里本来就会再用正则提取链接。

## 文件结构

- `main.py`：插件本体，监听、队列、下载都在里面
- `metadata.yaml`：插件信息
- `_conf_schema.json`：后台配置项
- `requirements.txt`：`aiohttp`


v0.2.5 修了发送失败：用户 AstrBot 里 astrbot.api.message_components 没有 MessageChain，下载 44/44 完成后发包和降级文本都因此失败。新代码只 import 一定有的 Plain / File，找不到 MessageChain 就直接用组件列表发，找不到时再按几个可能位置找一遍。


v0.2.6 修了 /ex_send 报错：chain_result 和 send_message 要的东西不一样，前者要组件列表，后者要 MessageChain 对象，之前统一包成 MessageChain 会在 result_decorate 里 len(result.chain) 报错。现在手动补发用列表、后台自动发送用 MessageChain。
