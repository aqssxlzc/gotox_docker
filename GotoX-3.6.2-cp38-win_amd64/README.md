# GotoX
- GotoX 修改自 goagent，其主要目的在于，当访问的网络服务出现问题，用户可以通过方便快速地添加更改规则来自行解决。
- 其特色，一是自动代理，可支持标准 HTTP/1.1 请求；二是可通过修改 TLS 的 SNI 扩展直连大部分网站。
- 主要使用 CFWorkers 服务作为后端代理，也支持 HTTP/SOCKS4/SOCKS5 代理，两者处于同等地位。SOCKS 代理支持认证。
- 配置 CFWorkers 代理时需使用完整域名，支持绑定自定义域名。
- 运行时可一直维护一个较小但快速的 GWS IP 列表（用于访问谷歌网络服务）。

# 可用性（2020/05/03）
- 谷歌又调整服务器了，可简单直连的 GWS IP 数量急剧下降，大部分 IP 会触发验证而无法顺利使用，重新筛选会耗费较长时间，请耐心等待。
- 由于谷歌政策调整，未绑定信用卡的 Cloud 账户 5 月 31 日后将无法使用其免费 GAE 服务，因此不再继续改进 GAE 服务端。
- 增加 CloudFlare Workers 作为新的远程代理服务器，测试中。

# 安全性
- 由于平台限制，对于通过 GAE 的 https 流量，GotoX 使用自动生成的证书作为凭证，采取中间人方法进行代理；对于需要修改（某些自动代理规则需要）的 https 流量也是如此，不论其是否通过 GAE。
- GotoX 使用 sha256 算法生成证书，只要你不把生成的[私钥文件](#导入证书)泄露出去，就不会有第三方能通过它们对你进行中间人攻击。
- 默认配置时，GotoX 会验证 GAE 服务器的中级证书是否为谷歌 [GIAG3](https://pki.goog/gsr2/GIAG3.crt)，同时会在 AppID 请求 https 网址时验证其证书有效性。
- 使用 GAE 代理也就意味着：你需要**信任谷歌和你所使用的 AppID 服务端的权限者**，他们能够窥探和修改通过 GAE 代理的流量信息。
- **为防止被滥用，谷歌在 GAE 代理的头字段中会包含了你使用的 AppID 和原始请求的 TraceID 信息，请慎记之。**
- 以上描述也适用于 CFWorkers。
- 支持通过 https 代理协议连接 GotoX，然而非加密后端规则（非 GAEProxy、HTTPS）的 http 连接从代理连出去仍然是普通 http 连接，这只是作为一个方法验证来实现。

# 部署服务端
- CFWorkers 服务端部署非常简单，可参看 wiki 简易教程。
- 注册和部署时，可尝试直接以默认配置运行本代理使用；如果无法顺利进行，请使用 VPN、Shadowsocks 等其它代理重新开始。
    - **警告**：不建议使用未知来源的兼容服务端，它们**可能会记录你的各种信息，甚至更改你的流量**以达到更危险的目的。
- **相关链接**
    - 简易教程 https://github.com/SeaHOH/GotoX/wiki/简易部署教程：CloudFlare-Workers
    - 常见问题 https://github.com/SeaHOH/GotoX/wiki/常见问题 （待更新）

# 使用
- ### 主要配置
    - 配置文件位于 `config` 文件夹，使用文本编辑器修改，不要使用 Windows 记事本。
    - 只有 `Config.ini` 支持 `Config.user.ini` 用户配置。
    - 通过阅读各个配置项目的注释，即可理解其用途。
    - 需事先提供由**其它扫描工具**取得一个较大的**可用的** GAE/GWS IP 列表以供筛选，放入 `data/ip.txt` 或 `data/ip_ex.txt` 中。
        - 格式为每行一个完整 IP；
        - 每次修改或新建以上两个文件时都会自动进行备份，只有一份，会被后来的覆盖；
        - `ip_ex.txt` 中的 IP 会优先使用，同时会自动并入 `ip.txt`；
        - `ip_ex.txt` 文件会在修改后大约二至十二小时内被删除，其 IP 优先使用也同时失效；
        - `ip_del.txt` 文件负责记录根据统计数据判断生成永久屏蔽 IP ；
        - 如果 `ip_ex.txt` 新加 IP 包含永久屏蔽 IP，会自动从 `ip_del.txt` 删除重置。
        - 其余 `ip_` 开头的文件为统计数据，可以删除它们以重置状态。
        - **注意**：首次使用或长时间未运行时，请在启动后等待 1-5 分钟，让 GotoX 完成 IP 筛选。
        - 更多细节请查看配置文件注释。
    - 也可以在 **［gae/iplist］** 配置中指定使用固定的 GAE IP 列表，不再持续进行 IP 检查筛选。
- ### 自动化
    - 自动代理规则和 IP 列表文件可以在运行时替换，无需重启 GotoX。
    - 直连和转发规则失败后，会根据条件判断是否尝试使用临时规则。
    - 可设置三级 DNS 查询优先级：系统设置、**［dns/server］** 配置、DNS-over-HTTPS API。
- ### 使用 IP 列表
    - google IP 列表名称“**google_gae、google_gws**”选项由代理自身维护，无需用户填写；
    - 用户可以使用其它名称配置自己的 IP 列表，以供自动规则使用。
    - “**google_gae**”，用于 GAE 代理，也可用于 google_gws，为保证代理顺畅一般规则不使用这个列表；
    - “**google_gws**”是未分类的 gws 服务器，可用于大部分 google 域名直连规则（默认配置），使用转发规则有可能出现证书错误。
    - 特殊谷歌主机名需自行测试调整，如文件上传服务器等。
    - **相关链接**
        - 如何区分各种 Server 端｜思起（转） https://github.com/SeaHOH/GotoX/wiki/GServers
        - 如何区分各种 Server 端｜思起（原） https://blog.aofall.com/archives/7.html
- ### 代理端口
    - 自动代理端口需自行配置规则，可根据需要自动分配链接路径，推荐使用（开发动力之一）；
    - 要使用自动代理请先仔细阅读配置规则说明，由于未添加完全的检测，错误规则可能导致程序出错或非预期的代理结果；
    - 活动端口完全使用指定的代理（如 CFW），只有当遇到不支持的方法时转用直连，如果此网络资源处于屏蔽状态连接会失败。
    - 这两个端口还支持 https 方式代理，建议分别设置代理协议，https 连接还是使用 http 方式代理，否则会形成双重加密，不仅耗费资源还可能被 Qos 限速；
    - 使用 https 方式代理，需设置一个 IP 作为默认主机名称，详见配置文件 **[listen/iphost]** 项注释。
    - 可以直接配置使用自动代理端口，也可以用浏览器插件或其它工具进行调度。
    - **相关链接**
        - 自动代理动作介绍 https://github.com/SeaHOH/GotoX/wiki/自动代理动作介绍
- ### 自定义 SNI 扩展
    通过改变 TSL 的 SNI 扩展，可以直接连接大部分服务器；此时应配合正确的 DNS 服务，或者使用内置的 DNS-over-HTTPS 服务。同时也支持使用原主机名验证服务器证书，详见相关链接。
    - **相关链接**
        - 如何使用伪造 SNI 的功能 https://github.com/SeaHOH/GotoX/wiki/如何使用伪造-SNI-的功能
- ### 用户认证
    支持以下方法，可设置免验证 IP 白名单。
    - **Basic 方法**认证。
        - 优点是支持广泛，基本不会因出错而无法使用；
        - Basic 方法是**不安全**的认证方法，不建议在外网使用。
    - 自写的 IP 认证。
        - 优点是安全性大大提升，但有个例外，见下面；
        - 需登录，登录时强制使用加密链接；
        - 登录后，如果连续 30 分钟没有发起任何请求，登录将会失效；
        - **重要**：由于只认 IP，多台主机可以通过一个处于成功登录状态的 IP 来使用本代理，所以请谨慎分享代理地址；
        - 同一个用户，只有最新登录成功的 IP 才能通过认证使用代理。
- ### 导入证书
    除了非 https 连接和转发（forward）https 连接外，其余代理连接都要求客户端导入自签证书。
    - 成功运行后会创建独一无二的 CA 证书，证书名称为：“**GotoX CA**”。
    - 配置好浏览器代理后，在地址栏输入“**http://gotox.go/** ” 即可安装或下载 CA 证书；
    - 也可在 `cert` 文件夹找到 `CA.crt` 证书文件；
    - 暂时只支持 Windows 启用自动导入和删除功能，其它系统如有需求请手动删除老旧证书。
    - **重要**：
        - `CAkey.pem` 包含完整的 CA 证书和密钥信息，**千万保护好此文件不要泄漏**。如果有运行多个 GotoX 副本的需要，保证此文件一致即可正常使用同一个 CA 证书。
        - 部分手机应用可能不会使用此证书导致验证失败。
    - **相关链接**
        - 手动导入证书 https://github.com/XX-net/XX-Net/wiki/GoAgent-Import-CA
- ### 本地服务
    提供一个简单的支持加密链接的静态 web 服务器。
    - 不提供单独端口，**无法直接访问**，需要在设置了 GotoX 代理的情况下访问。
    - 主机名为“gotox.go”或 GotoX 代理地址，如果是在本机运行 GotoX 且未设置成代理例外，那也可以是本机地址（`127.0.0.1`、`localhost`）。
    - 路径根目录为程序的 web 文件夹。
    - 路径为目录时不自动发现 index 文件，显示目录列表以替代。
    - 返回头部支持常见文件类型，不包含缓存信息。
- ### 辅助工具
    - Windows 下提供一个系统托盘辅助工具。
        - 使用发布的便携版 Python 可以从 `GotoX.vbs` 或快捷方式启动（使用 `create_shortcut.vbs` 生成于桌面）；
        - 使用安装版 Python 可直接运行 `launcher/start.py` 启动。
        - 可以打开 GotoX 配置文件；
        - 可以选择使用何种类型的 gevent 事件循环；
        - 可以下载生成直连 IP 数据库，**其它系统**需直接运行 `launcher/buildipdb.py` 脚本；
        - 可以下载生成直连域名数据库，**其它系统**需直接运行 `launcher/builddomains.py` 脚本；
        - 隐藏或显示窗口；
        - 监视和设置系统代理（仅适用于局域网连接，其它如直接拨号连接等可能无效）；
        - 重新载入 GotoX。
    - 作者现在没有条件为其它系统开发类似工具，欢迎感兴趣者分享代码。
- ### 小技巧
    - 对于不支持 GAE/CFW 出口的网址
        - 可在**转发（forward）或直连（direct）** 规则中设置成**反向代理** IP；
        - 或在**其它代理（proxy）** 规则中设置成 **SOCKS 代理**（格式见 `ActionFilter.ini`）。
    - 反向代理一般**不支持**非加密链接，请**慎用**支持非加密链接的反向代理！
    - 尽量不要在 CFW 代理中大量下载（包括多线程）较大的文件，单个 CFWorker 可用内存上限仅有 128MB。

# 兼容性
- CPython 3.6/3.7/3.8 已测试，其它版本则无法保证持续运行正常。
- 必须组件：
    - gevent 1.3.0 及以上
    - pyOpenSSL 16.0.0 及以上
    - dnslib 0.8.3 及以上
    - PySocks 1.5.4 及以上
- 可选组件：
    - brotlipy 0.5.0 及以上
- 发布将提供包含 Windows CPython 3.8 环境的便携版本。
- 由于自己只使用 Windows，所以其它系统不保证能正常使用。如果有需求作者会尽量修改，但这需要有人帮助测试反馈。

# 其它
- 支持修改请求和响应（低）。
- 支持直连（direct）使用自动多线程（低）。
- 暂不打算提供前置代理功能，请使用转发到后端代理。
- 不会提供对 HTTP/2 的直接支持，对我个人来说带来的改善无法与付出对等，代码不是我的本行。其部分特性可用多 IP、多 AppID 以及 keep-alive 的组合来替代，主要损失的是头部的压缩支持，其次某些情况下延迟更大些，尤其是对应 IP 稀少或只有 1 个 IP 时。**仅在服务器支持 HTTP/2 时对比。**
    - **GAE** 问题不大，实际的头部也会压缩。
    - **直接转发**完全等于直连，是否支持看客户端。
    - **转发代理**还要看代理服务器是否支持。
    - 其余的就完全不支持了。