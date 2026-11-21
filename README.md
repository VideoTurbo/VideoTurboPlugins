# VideoTurbo 插件

[VideoTurbo](https://github.com/VideoTurbo/VideoTurbo) 的插件集合，按类别分目录存放：

| 目录 | 类别 |
| --- | --- |
| [`cloud_gpu/`](cloud_gpu) | 云 GPU 平台插件 |
| [`media_source/`](media_source) | 网盘媒体库来源插件 |

## 云 GPU 平台插件

让 VideoTurbo 直接管理云 GPU 平台上的机器：查看、开机、关机、新建和销毁。每个平台一个插件文件。

| 平台 | 版本 | 插件文件 | 插件地址 |
| --- | --- | --- | --- |
| 晨羽智云 | 0.1.0 | [`cloud_gpu/chenyu.py`](cloud_gpu/chenyu.py) | `https://github.com/VideoTurbo/VideoTurboPlugins/blob/main/cloud_gpu/chenyu.py` |
| 智川云（GPUHome） | 0.1.0 | [`cloud_gpu/gpuhome.py`](cloud_gpu/gpuhome.py) | `https://github.com/VideoTurbo/VideoTurboPlugins/blob/main/cloud_gpu/gpuhome.py` |

### 安装

**从插件仓库安装（推荐）**：打开 VideoTurbo 控制台“设置 → 插件 → 插件仓库”，添加本仓库地址
`https://github.com/VideoTurbo/VideoTurboPlugins`，在列表中找到要用的插件点“安装”，核对 SHA-256 后确认，然后从下面第 3 步继续。

**按地址安装**：

1. 复制上表中要安装的插件地址。
2. 打开 VideoTurbo 控制台“设置 → 插件”，粘贴地址，点“安装”，核对 SHA-256 后确认。
3. 打开“云算力 → 云平台”，点“添加”，在“接入模式”中选择平台，填写登录信息并保存：
   - 晨羽智云：填 API Key，在晨羽智云控制台“API管理”创建。
   - 智川云：填手机号和密码，密码不会保存。

插件不会自动更新。打开“设置 → 插件”时，控制台会检查插件地址上有没有新内容，有新版本时卡片上显示“可更新到 vX.Y.Z”和“更新”按钮，核对 SHA-256 后确认即可。

### 发布新版本

修改插件后，同时把插件类中的 `version` 和上表中的版本号加一。插件类中的 `icon` 是控制台卡片上显示的图标（`data:image/…` URI 或内联 SVG 源码，最长 64 KB）。

然后在仓库根目录运行 `python tools/build_index.py`，重新生成 `plugins.json`（控制台的“插件仓库”按它列出插件）一并提交；
`python tools/build_index.py --check` 只检查它是否最新，可用于 CI。新增插件文件放进对应类别的目录即可，脚本会自动收录。

### 功能对比

| 功能 | 晨羽智云 | 智川云 |
| --- | --- | --- |
| 查看、开机、关机、销毁实例 | 支持 | 支持 |
| 用镜像新建实例 | 私有镜像 | 私有、基础、社区镜像，按量计费 |
| 显示价格和库存 | 支持 | 支持，按地区和卡数显示 |
| 保存镜像 | 支持 | 支持，名称最多 10 个字 |
| 自动部署、空闲自动关机 | 支持 | 支持 |
| VideoTurbo 断线时由平台自动关闭空闲机器 | 支持 | 不支持 |

## 网盘媒体库来源插件

把网盘作为 VideoTurbo 的媒体库：主程序通过插件浏览目录、扫描影片、发布字幕，GPU 机器通过插件直接从网盘下载影片，
影片不经过主程序。每种网盘一个插件文件。

| 网盘 | 版本 | 插件文件 | 插件地址 |
| --- | --- | --- | --- |
| OpenList（兼容 AList v3） | 0.1.0 | [`media_source/openlist.py`](media_source/openlist.py) | `https://github.com/VideoTurbo/VideoTurboPlugins/blob/main/media_source/openlist.py` |

### 安装和设置

1. 按上文“云 GPU 平台插件”的方法从插件仓库或按地址安装插件，核对 SHA-256 后确认。
2. 在“设置 → 插件”的 OpenList 卡片上点设置按钮，填写：
   - **OpenList 地址**：主程序访问 OpenList 的地址，例如 `http://192.168.1.10:5244`。
   - **登录方式**：
     - 令牌：OpenList“设置 → 其他”中的令牌，拥有管理员权限，最简单；
     - 用户名和密码：账号需要有读取、写入和改名权限，不能开启两步验证；登录后得到的令牌保存在插件状态里，过期后自动重新登录；
     - 游客：不登录，只适用于开放了游客访问的 OpenList，游客通常没有写入权限，无法发布字幕。
   - **影片地址**：GPU 机器下载影片、主程序读取影片信息时使用的地址。
     - OpenList 下载链接（`/d/`，默认）：由 OpenList 按存储设置跳转到网盘直链或代理转发，兼容性最好；
     - 网盘直链（`raw_url`）：不经过 OpenList，但部分网盘的直链需要特定请求头或绑定请求来源，GPU 机器可能无法下载。
   - **GPU 机器使用的地址**（可选）：主程序用容器名或 `127.0.0.1` 访问 OpenList 时，填写 GPU 机器能访问的局域网地址。
   - **目录密码**（可选）：媒体库所在目录在 OpenList 中设置了访问密码时填写。
3. 打开“设置 → 媒体库”，添加媒体库时来源选择 OpenList，再选择影片所在的文件夹。

### 注意事项

- 插件列目录时使用 OpenList 的目录缓存，不强制刷新。在 OpenList 之外往网盘里添加的影片，要等 OpenList 的目录缓存过期后才会被扫描到；
  可以在 OpenList 的存储设置里调短“缓存过期”时间，或在 OpenList 网页上手动刷新该目录。通过插件发布的字幕和改名由 OpenList 自己更新缓存。
- 网盘提供文件哈希时，插件用它认出改名或移动过的影片；不提供时，改名后的影片按新影片处理。
- 改名跨越 OpenList 的不同存储时，OpenList 会把移动作为后台任务执行，插件会提示“任务完成后重新扫描媒体库”。

## 开发

运行测试需要 VideoTurbo 的源码，在 VideoTurbo 仓库目录下执行：

```bash
uv run pytest --rootdir /path/to/this/repo /path/to/this/repo/tests
```

## 许可证

[AGPL-3.0-or-later](LICENSE)。分发修改版，或把修改版作为网络服务提供给他人时，需要以相同许可证公开源代码。

🚫禁止将本项目用于制作、传播违法内容或侵犯他人权益。
