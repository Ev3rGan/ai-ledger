# 香港服务器购买方案调研（2026-08-13）

> 调研日期：2026-08-13。价格、可售状态、税费和镜像库存均可能变化；本报告只把厂商官网与官方文档中能核实的内容写成确定事实。没有登录三个云账号，也没有完成支付，因此“可下单”表示官方当前目录仍在售且存在购买入口，不代表指定账号、可用区在付款时一定有库存。

## 结论与建议

为完成 [GitHub Issue #7：Benchmark the Hong Kong runtime](https://github.com/Ev3rGan/ai-ledger/issues/7) 的同条件对比，应先各买 **1 个月**，不要先签年约；关闭不需要的自动续费，并在付款前保存配置页、含税金额和订单截图。该做法也符合 [ADR 0005](../adr/0005-deploy-the-mvp-as-a-small-hong-kong-service.md) 中“香港单机、4 GB 内存、约 USD 15–30/月、先实测后决策”的范围。

推荐下单组合如下：

| 厂商 | 建议套餐 | 官网标价 | 计费与承诺 | 存储 | 公网套餐 | 当前可下单判断 | 购买入口 |
|---|---|---:|---|---:|---|---|---|
| 腾讯云 Lighthouse `ap-hongkong` | 香港 Linux 锐驰型 2 vCPU / 4 GiB | **CNY 95/月** | 包年包月预付费；该 2c4g 规格不在页面所列长期折扣门槛内，先买 1 个月 | 60 GB SSD | 峰值 200 Mbps，不限流量 | 官方 2026-06-15 价格表仍列为在售；最终库存和应付额需登录确认 | [Lighthouse 购买页](https://buy.cloud.tencent.com/lighthouse?from=product_page) |
| AWS Lightsail `ap-east-1` | Linux/Unix、public IPv4、Medium-4GB，2 vCPU / 4 GB | **USD 24/月上限** | 按小时计费，累计至月套餐上限；停止实例仍计费，删除才停止 | 80 GB SSD | 香港实际 2 TB/月；超额出站 USD 0.09/GB | Lightsail 官方支持香港；区域默认未启用，账号需先 opt in | [Lightsail 控制台](https://lightsail.aws.amazon.com/ls/webapp/home) |
| 阿里云轻量应用服务器 `cn-hongkong` | `swas.s.c2m4s50b1.linux`，2 vCPU / 4 GiB | **中国站动态价：登录后确认**；国际站公开参考 **USD 16/月** | 预付费订阅；中国站默认 1 个月且默认自动续费，可改 1/3/6 个月或 1/2/3 年 | 50 GiB | BGP，峰值最高 200 Mbps，不限流量 | 当前“在售实例规格”目录包含香港；中国站实时价和库存登录后才能确认 | [中国站预填购买页](https://swasnext.console.aliyun.com/buy?amount=1&autoRenew=false&duration=1&planId=swas.s.c2m4s50b1.linux&regionId=cn-hongkong) |

三台机器统一选择 **Linux、x86_64/amd64、纯系统镜像 Ubuntu 22.04 LTS**，不要选择厂商预装 Docker/应用镜像。随后按 [Docker 官方 Ubuntu 安装说明](https://docs.docker.com/engine/install/ubuntu/) 在三台机器安装并锁定同一个 `docker-ce`、`docker-ce-cli` 和 Compose plugin 版本。Ubuntu 22.04（Jammy）和 amd64 在 Docker 官方支持矩阵内；最终仍需在每个购买页确认该地域当时能选到该镜像与架构。

### HTTPS 域名与 SSH 交接方案

不需要购买三个独立域名。若已有一个可控制 DNS 的域名，为三台服务器分别创建三个 A 记录即可，例如 `tencent-hk.example.com`、`aws-hk.example.com`、`aliyun-hk.example.com`，各自指向对应公网 IPv4。每台开放 TCP 80/443 后，可使用同一份 Caddy 配置模板完成 TLS 终止和反向代理；[Caddy 官方 HTTPS 指南](https://caddyserver.com/docs/quick-starts/https) 说明，只要公网域名已解析到服务器且 80/443 可达，Caddy 会自动申请和续期受信任证书。已有域名时，这部分不增加证书费用；若没有域名，域名注册费取决于后缀和注册商，未计入本次服务器报价。

SSH 访问应在三家购买页都绑定或导入公钥，并只把主机地址、SSH 用户名和主机指纹交接给执行测试的人；私钥保留在本地，不粘贴到聊天、Issue 或仓库。首次登录后保存 `uname -m`、`/etc/os-release` 和 SSH host key fingerprint，确认三台都是 `x86_64` 且镜像版本一致后再安装 Docker。

## 1. 腾讯云 Lighthouse（香港）

### 套餐与价格

腾讯云 [Lighthouse 套餐价格表](https://cloud.tencent.com/document/product/1207/73452) 标注更新于 2026-06-15，并说明未列出的旧套餐已经下线。香港 Linux 当前至少有两档满足 2 vCPU / 4 GiB：

| 套餐 | 月价 | 系统盘 | 公网 |
|---|---:|---:|---|
| 香港锐驰型 2c4g | CNY 95 | 60 GB SSD | 峰值 200 Mbps，不限流量 |
| 香港入门型 2c4g | CNY 90 | 70 GB SSD | 峰值 30 Mbps，2,048 GB/月 |

建议选 **CNY 95 锐驰型**：每月只多 CNY 5，换取不限流量和更高峰值上限，更适合反复跑下载、SSE、容器拉取和源站出口探针。这里的 200 Mbps 只是峰值上限而非带宽承诺；腾讯云 [锐驰型说明](https://cloud.tencent.com/document/product/1207/115752) 明确说高峰期可能受资源争抢限制，因此不能把它当成持续吞吐保证。

若改选 CNY 90 入门型，2,048 GB 配额只统计出站，月初重置；香港超额流量按 CNY 1/GB。配额口径和峰值定义见 [网络与流量说明](https://cloud.tencent.com/document/product/1207/79254)。

Lighthouse 是包年包月预付费产品；[快速创建实例](https://cloud.tencent.com/document/product/1207/44548/) 要求完成注册、实名认证和充值后购买。价格页虽然列出 6–11 个月 88 折、12 个月及以上 85 折，但同页限制条件显示香港入门/通用型要从 2c8g 起、锐驰型要从 4c8g 起才享受对应折扣，因此本次 2c4g 不应预估长期折扣。

### 税票与下单注意事项

腾讯云 [电子发票说明](https://cloud.tencent.com/document/product/555/7434) 显示，消费后可以申请数电普通发票或数电专用发票；个人实名只能开普通发票，企业实名可以开普通或专用发票，云服务税率列为 6%。价格表没有在同一处明确说明 CNY 95 是否已经包含全部结算税额，因此应以支付页和实际发票为准，不额外推算含税价。

- 购买：打开 [Lighthouse 购买页](https://buy.cloud.tencent.com/lighthouse?from=product_page)，地域选择“中国香港”，Linux，锐驰型 2c4g，1 个月。
- 镜像：腾讯云的 [镜像更新记录](https://cloud.tencent.com/document/product/1207/47042) 证明曾上线 Ubuntu 22.04 LTS；当前地域可选项以购买页为准。不要选预装 Docker 的应用镜像，以免基础 OS 或 Docker 版本与另外两台不同。
- 可售结论：官方当前价格表仍列该规格，购买页也存在；未登录，无法证明付款时库存。

### 中国大陆访问风险

腾讯云明确提示，中国大陆与香港/海外地域之间的公网质量不作保证，可能出现高延迟和丢包；锐驰型也没有持续带宽 SLA。因此这台机器必须用 Issue #7 规定的同一台大陆测试机、同一时段、同一探针实测，不能仅凭“腾讯云”品牌推断大陆链路更好。

## 2. AWS Lightsail（香港）

### 套餐与价格

AWS [Lightsail 定价页](https://aws.amazon.com/lightsail/pricing/) 的 Linux/Unix public IPv4 2 vCPU / 4 GB 套餐为 **USD 24/月**，含 80 GB SSD 和标称 4 TB 流量。该页同时注明亚太（香港）等地域只获得表列流量的一半，所以 `ap-east-1` 的实际月度配额是 **2 TB**，不是 4 TB。[Lightsail bundles 文档](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html) 也列出 Medium-4GB 的 2 vCPU、4 GB、80 GB 和 USD 24 规格。

配额会累计入站和出站，但只有超出配额的出站流量收费；香港超额出站价格为 **USD 0.09/GB**，见 [Lightsail 数据传输说明](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-faq-data-transfer-allowance.html)。Lightsail 未给该套餐一个可直接用于三云横比的固定 Mbps 承诺，带宽应靠实测记录。

AWS [Lightsail 计费 FAQ](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-frequently-asked-questions-faq-billing-and-account-management.html) 说明实例按小时累计、最高不超过月套餐价；月中删除会按已使用小时计费。仅停止实例不会停止实例费，完成测试后必须删除实例和不再需要的附属资源。

### 税票与下单注意事项

AWS 的税额不是一个对所有买家通用的固定加成。[AWS 税务地址规则](https://aws.amazon.com/tax-help/location/) 说明税务地点按账号税务地址、付款方式地址和联系地址等信息确定；seller of record 也按账号所在地确定。AWS [账单和发票文档](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/getting-viewing-bill.html) 说明可在 Billing 控制台下载月度 PDF 发票以及适用的税务发票/补充文件。因此 USD 24 应视为套餐基础价，付款税额以该 AWS 账号的结算页和发票为准。

- 区域：官方 [Lightsail 地域列表](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-regions-and-availability-zones-in-amazon-lightsail.html) 包含 `ap-east-1`，但香港属于默认禁用的 opt-in Region；账号需先启用香港区域。
- 购买：进入 [Lightsail 控制台](https://lightsail.aws.amazon.com/ls/webapp/home)，创建实例，选择 Hong Kong、OS Only、Linux/Unix、public IPv4、2 vCPU / 4 GB（USD 24）。[官方创建步骤](https://docs.aws.amazon.com/lightsail/latest/userguide/getting-started.html) 提供了地域、镜像和套餐的选择流程。
- 镜像：选择 OS Only Ubuntu 22.04 LTS 和 x86_64；若控制台当时没有该镜像，不能用另一 Ubuntu 版本冒充同条件，应暂停下单或把三云统一到一个都可用的版本。
- 可售结论：服务与套餐均在官方目录内；账号区域 opt in、身份验证、配额及指定可用区容量仍需登录确认。

### 中国大陆访问风险

本次查到的 AWS 官方资料确认的是 Lightsail 在香港地域可用，并未给出“中国大陆公网访问香港 Lightsail”的专线、优化路由或时延保证。由此只能得出“不可预设质量”的结论，而不能推断一定好或一定差。应把大陆方向延迟、丢包、SSE 长连接、IPv4 可达性和晚高峰波动作为实测项。

## 3. 阿里云轻量应用服务器（香港）

### 套餐与价格

阿里云中国站的 [当前在售实例规格](https://help.aliyun.com/zh/simple-application-server/product-overview/instance-families/) 包含通用型 `swas.s.c2m4s50b1.linux`：2 vCPU、4 GiB、50 GiB 系统盘、无固定流量配额（即不另收流量费）、BGP、公网峰值最高 200 Mbps、1 个 IPv4，地域包含中国香港。官方同时提示通用型 CPU 性能和峰值带宽不作保证，高并发时可能受资源争抢影响。

中国站产品页的金额是动态询价/登录后展示，本次无法从公开页面确认一个当前 CNY 月价，故不得把旧促销价或历史价写成成交价。阿里云国际站 [SWAS 产品页](https://www.alibabacloud.com/en/product/swas?_p_lc=1) 当前公开展示同一 SKU 在香港的 **USD 16/月**，但这只是国际站账号的公开参考价。

中国站和国际站不是同一套账号与结算体系。官方 [Aliyun 与 Alibaba Cloud 账号差异](https://www.alibabacloud.com/help/en/account/aliyun-vs-alibaba-cloud) 说明，中国站通常使用中国大陆手机号注册、以 CNY 含税结算；国际站需要中国大陆以外手机号，通常以 USD/本地货币结算并另算适用税费。因此大陆账号应使用中国站购买链接并以登录后的 CNY 价格为准，不能拿 USD 16 要求中国站结算。

[创建服务器文档](https://help.aliyun.com/zh/simple-application-server/user-guide/create-a-server/) 说明，产品为订阅预付费，默认购买 1 个月且自动续费默认开启；可选 1/3/6 个月或 1/2/3 年。首次基准测试应选择 1 个月并关闭自动续费。

### 税票与下单注意事项

中国站 [申请发票说明](https://help.aliyun.com/zh/user-center/request-an-invoice) 明确表示产品价格和账单金额为含税价、不另收税；支持数电普通/专用发票，个人只能开普通发票，企业可按条件开普通或专用发票。官方 [发票 FAQ](https://help.aliyun.com/zh/user-center/support/faq-about-invoice-requests) 显示一般云产品常见税率为 6%，但最终以实际商品和发票为准。

国际站税费和发票规则不同。[国际站发票说明](https://www.alibabacloud.com/help/en/user-center/get-invoice-international-station) 说明税号、B2B/B2C 身份和所在地会影响税费；按量/预付订单的发票可在规定账期后下载。因此国际站 USD 16 也不应被表述成所有账号的最终含税支付额。

- 中国站购买：[预填香港、2c4g、1 个月、关闭自动续费的购买页](https://swasnext.console.aliyun.com/buy?amount=1&autoRenew=false&duration=1&planId=swas.s.c2m4s50b1.linux&regionId=cn-hongkong)。登录后再次检查 `regionId=cn-hongkong`、规格、续费开关、实时 CNY 价格和库存。
- 国际站购买（仅限已具备国际站账号）：[国际站 SWAS 购买入口](https://swas.console.alibabacloud.com/buy?commodityCode=swas_intl&regionId=cn-hongkong)。先选择同一 SKU，再将默认应用镜像改成纯系统镜像。
- 镜像：阿里云 [镜像目录](https://www.alibabacloud.com/help/en/simple-application-server/product-overview/images) 列出 Ubuntu 22.04、24.04，并说明镜像供应按地域变化、购买页为准。不要选择厂商 Docker 应用镜像，因为其基础系统和 Docker 版本会破坏三云同条件。
- 可售结论：当前在售目录明确包含该 SKU 和香港地域，且购买入口有效；中国站实时价格、账号资格和库存仍是登录门槛。

### 中国大陆访问风险

阿里云 [地域与网络连通性说明](https://help.aliyun.com/zh/simple-application-server/product-overview/regions-and-network-connectivity) 明确提示，中国香港及海外地域使用国际带宽，与中国大陆之间不是直连，访问可能出现高延迟、丢包甚至不可达。这个通用 BGP 套餐不能等同于针对大陆优化的专线产品，必须以固定大陆测试机的实测结果判断。

## 下单前统一检查清单

1. 三台均为香港地域：`ap-hongkong`、`ap-east-1`、`cn-hongkong`；均选择 public IPv4。
2. 三台均为至少 2 vCPU / 4 GiB、x86_64/amd64、纯 Ubuntu 22.04 LTS 系统镜像。
3. 记录订单页的 CPU、内存、系统盘、公网配额/峰值、购买周期、自动续费、税费和最终应付额；不要只保存产品宣传页。
4. 用同一脚本安装并锁定 Docker Engine 与 Compose 版本；启动后保存 `uname -m`、`/etc/os-release`、`docker version`、`docker compose version` 输出。
5. 为三台配置独立 HTTPS 子域名或等价的可用 TLS 反向代理；验证公网 80/443、证书链、自动续期和 HTTP 到 HTTPS 跳转。
6. 从同一台中国大陆探针机、同一时段对三台执行 Issue #7 的网络、SSE、源站出口、模型 API、OAuth、资源与成本探针；峰值带宽不能替代实测。
7. 基准测试结束后，AWS 需删除实例才停止实例费；腾讯云和阿里云关闭自动续费并检查快照、数据盘、备份等附属资源是否另收费。

## 仍需在登录后确认的变量

- 腾讯云：指定时刻的香港库存、支付页是否有账号专属优惠、最终含税应付额、Ubuntu 22.04 x86_64 镜像是否可选。
- AWS：账号是否已启用 `ap-east-1`、服务配额/容量、Ubuntu 22.04 x86_64 镜像、账号所在地对应税费。
- 阿里云：中国站该 SKU 的实时 CNY 月价、库存和账号优惠；国际站 USD 16 是否仍适用于该账号，以及所在地税费。

这些不确定项是动态定价、账号资格或登录后库存造成的，不能用第三方报价或历史促销信息替代。
