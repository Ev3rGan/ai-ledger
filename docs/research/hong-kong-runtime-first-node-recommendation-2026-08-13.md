# 香港运行时首节点顺序建议（2026-08-13）

> 调研日期：2026-08-13。本文只用于在无法同时购买三台服务器时决定首个试验节点，不替代 Issue #7 的三云横向基准。价格、库存、税费、区域启用状态和镜像库存会变化；下单页显示的信息优先于本文。

## 决策结论

建议按以下顺序购买和测试：

1. **腾讯云 Lighthouse 香港 `ap-hongkong`，Linux 锐驰型 2 vCPU / 4 GB（CNY 95/月）**。
2. **AWS Lightsail 香港 `ap-east-1`，Linux/Unix public IPv4，2 vCPU / 4 GB（USD 24/月上限）**。
3. **阿里云轻量应用服务器 `cn-hongkong`，`swas.s.c2m4s50b1.linux`（中国站实时价以登录后的下单页为准）**。

首选腾讯不是因为可以预判其中国大陆链路一定最好，而是它在当前官方事实中给出最好的长期综合先验：CNY 95 的月费低于项目 USD 15–30 预算上限，2 vCPU / 4 GB / 60 GB SSD 已满足规格门槛，200 Mbps 峰值且不限流量；相比 AWS 2c4g，没有已公开的 20% 持续 CPU 基线限制。[腾讯云香港价格表](https://cloud.tencent.com/document/product/1207/73452)列出该在售套餐，[锐驰型说明](https://cloud.tencent.com/document/product/1207/115752)明确说明不限流量，同时也声明 200 Mbps 只是峰值而非业务承诺。符合资格时，同一实名主体每个套餐类型首次退还有 5 天无理由全额退款机会；这能降低“先买一台再决定”的损失，但资格必须在购买前核实，不能预设一定可退。[腾讯云退还规则](https://cloud.tencent.com/document/product/1207/44582)

腾讯的主要试验风险不是公开配置，而是当前能找到的腾讯实例元数据说明面向 CVM，未找到明确把 Lighthouse 纳入 `metadata.tencentyun.com` 实例身份接口承诺的 Lighthouse 产品文档。Issue #7 workload 会把元数据失败视为硬失败，所以购买前最好先通过工单确认这两个路径在香港 Lighthouse 宿主机与容器中可用：

```text
http://metadata.tencentyun.com/latest/meta-data/instance-id
http://metadata.tencentyun.com/latest/meta-data/placement/region
```

AWS 排第二，适合在腾讯主要败于大陆链路时迅速验证不同网络。AWS 明确记录了 Lightsail 香港区、Lightsail IMDSv2 和实例身份文档，现有 workload 的身份探针有产品级依据；实例按小时计费，删除后停止实例费，一天左右的失败试验不必承担完整月费。[AWS Lightsail 地域列表](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-regions-and-availability-zones-in-amazon-lightsail.html)列出香港 `ap-east-1` 并注明该区域默认禁用；[Lightsail IMDS 文档](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-instance-metadata.html)明确支持 IMDSv2；[Lightsail 计费文档](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-frequently-asked-questions-faq-billing-and-account-management.html)说明按小时累计至月度上限、停止仍计费、删除才停止实例费。但 [Lightsail CPU 基线文档](https://docs.aws.amazon.com/lightsail/latest/userguide/baseline-cpu-performance.html)把该通用套餐的每 vCPU 持续基线列为 20%；短测可能使用 burst capacity 掩盖持续负载降速，因此它不是最稳妥的首个长期部署候选。

阿里排第三。官方规格目录确认 `swas.s.c2m4s50b1.linux` 为 2 vCPU、4 GiB、50 GiB、一个固定 IPv4、BGP、最高 200 Mbps且不收流量费，并包含中国香港；同时官方明确 CPU 型号、磁盘性能和峰值带宽均不构成性能承诺，资源争抢可能引起性能波动和丢包。[阿里云轻量服务器规格目录](https://help.aliyun.com/zh/simple-application-server/product-overview/instance-families/)给出了这些规格与限制。现有 workload 使用的安全加固元数据和实例身份文档有明确的[ECS 官方文档](https://help.aliyun.com/zh/ecs/user-guide/use-instance-identities)，但本次没有找到同等明确的 SWAS 产品文档保证；中国站实时 CNY 价格也需要登录确认。

如果腾讯购买前无法从官方确认 Lighthouse 元数据路径，或者账号不具备首退资格且不愿承担一个月试错成本，则把 AWS 提升为首个 pilot。若腾讯败于 CPU、磁盘或持续资源争抢，不应盲目转 AWS，因为 AWS 2c4g 也有明确的持续 CPU 基线；此时应先考虑阿里同规格，或重新评估非轻量型 CVM/ECS。

## 三个候选的官方事实对照

| 项目 | 腾讯 Lighthouse（首选） | AWS Lightsail（第二） | 阿里 SWAS（第三） |
|---|---|---|---|
| 香港地域 | 中国香港，workload 期望 `ap-hongkong`；2c4g 锐驰型在当前官方价格表中。[官方价格表](https://cloud.tencent.com/document/product/1207/73452) | `ap-east-1`；官方支持但默认禁用，账号先 opt in。[官方地域文档](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-regions-and-availability-zones-in-amazon-lightsail.html) | 中国香港；在售规格目录包含目标 SKU。[官方规格目录](https://help.aliyun.com/zh/simple-application-server/product-overview/instance-families/) |
| CPU / 内存 / 系统盘 | 2 vCPU / 4 GB / 60 GB SSD；不承诺底层 CPU 型号。[官方价格表](https://cloud.tencent.com/document/product/1207/73452)、[常见问题](https://cloud.tencent.com/document/product/1207/44569/) | 2 vCPU / 4 GB / 80 GB SSD；通用套餐每 vCPU 持续基线 20%，可 burst。[官方 bundle 表](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html)、[CPU 基线](https://docs.aws.amazon.com/lightsail/latest/userguide/baseline-cpu-performance.html) | 2 vCPU / 4 GiB / 50 GiB；只支持 x86，CPU 型号不承诺。[官方规格目录](https://help.aliyun.com/zh/simple-application-server/product-overview/instance-families/) |
| 基础价格与承诺 | CNY 95/月，包年包月预付；2c4g 不满足官方长期折扣最低规格。[官方价格表](https://cloud.tencent.com/document/product/1207/73452) | USD 24/月上限，按小时计费；删除后按小时结算。税费依账号而变。[计费文档](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-frequently-asked-questions-faq-billing-and-account-management.html) | 中国站实时价需登录；订阅预付费，购买页为最终依据。[创建服务器文档](https://help.aliyun.com/zh/simple-application-server/user-guide/create-a-server/) |
| 公网流量 / 带宽 | 200 Mbps 峰值、不限流量；峰值不是业务承诺，争抢时可能受限。[锐驰型公告](https://cloud.tencent.com/document/product/1207/115752) | 标称 4 TB，香港只有表列额度的一半，即 2 TB/月；没有可用于本次比较的固定 Mbps 承诺。[官方定价页](https://aws.amazon.com/lightsail/pricing/) | 无固定流量、不收流量费、BGP、最高 200 Mbps；CPU、盘、带宽均可能波动，峰值不是业务承诺。[官方规格目录](https://help.aliyun.com/zh/simple-application-server/product-overview/instance-families/) |
| 中国大陆链路 | 官方明确提示从中国内地访问港澳台/境外可能出现较大延迟和丢包。[锐驰型公告](https://cloud.tencent.com/document/product/1207/115752) | 官方只确认香港服务可用，没有大陆访问优化或时延保证；必须实测。 | 官方明确提示香港等中国大陆外地域使用国际带宽，可能从大陆出现高延迟；峰值带宽不等于稳定质量。[创建服务器文档](https://help.aliyun.com/zh/simple-application-server/user-guide/create-a-server/) |
| 当前 workload 的元数据匹配 | **待购买前确认。** 当前找到的 Lighthouse 官方资料未明确承诺 workload 使用的两个路径。 | **明确匹配。** Lightsail 官方支持 IMDSv2和实例身份文档；容器访问需把 metadata response hop limit 调到合适值，同时保持 IMDSv1 禁用。[Lightsail IMDSv2 文档](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-configuring-instance-metadata-service.html) | **待购买前确认。** 官方安全加固实例身份文档属于 ECS；未找到 SWAS 的同等承诺。[ECS 实例身份文档](https://help.aliyun.com/zh/ecs/user-guide/use-instance-identities) |
| 首轮试验的损失上限 | 预付一个月；符合账号及套餐资格时可使用首次 5 天无理由退还，必须下单前确认。[退还规则](https://cloud.tencent.com/document/product/1207/44582) | 最低且最确定：按小时，确认失败后立即删除实例及附属资源。 | 至少一个预付月；中国站实时价和元数据支持仍待确认。 |

所有三家都只给出轻量产品的规格或峰值，不能从官方说明推出哪家在用户所在地、运营商和测试时段一定更快。首节点建议优化的是“哪台通过后最适合直接长期采用，同时把失败损失控制住”，不是用宣传参数代替真实网络测量。

## 单节点策略与 Issue #7 原验收的差异

用户提出的“先买一台，达到预定门槛就采用；质量较差时再买下一台”可以作为预算受限的 **pilot / qualification** 策略，也符合 [ADR 0005](../adr/0005-deploy-the-mvp-as-a-small-hong-kong-service.md) 允许单台 4 GB 香港节点承载个人 MVP 的最终部署形态。

但是，它**不能直接满足 [Issue #7 当前验收](https://github.com/Ev3rGan/ai-ledger/issues/7)**：Issue #7 分支中的固定三云基准协议和现有 comparator 均要求恰好一份腾讯、AWS、阿里的结果；三份结果必须使用相同 Git commit、workload 与 PostgreSQL 镜像 SHA-256、协议 SHA-256、固定大陆观察者、不同节点和 URL，并落在同一个 24 小时观察窗口。少一份就会 fail closed，也不会输出正式推荐。

因此应这样记录状态：

- 单节点通过：可以形成“腾讯 Lighthouse pilot 合格，暂作 MVP 运行节点”的运维决策和一份原始 JSON 证据。
- 单节点不通过：进入下一候选，不把失败解释为整个香港方案失败。
- 单节点通过后停止购买：Issue #7 的“三云横向比较”仍未完成；不要运行或伪造三输入 comparator 报告，也不要以原验收标准关闭 Issue。
- 若团队决定永久接受顺序试验替代三云横评，应先在 Issue 中明确变更验收范围或另建后续 ticket，而不是让实现结果悄悄偏离既定协议。

## 测试前冻结的门槛

为避免看到首个结果后移动标准，购买前把以下门槛连同观察者标签、ISP、城市、日期、时间段和镜像 SHA 一起写入试验记录。

### 硬门槛：任一失败就测试下一家

这些门槛沿用现有 workload 的 fail-closed 语义：

1. 月度基础价的最新官方证据不超过 **USD 30**，证据日期不超过 31 天；税费和必要附属资源另行记录。
2. metadata 返回非空实例 ID 和准确香港地域：AWS `ap-east-1`、腾讯 `ap-hongkong`、阿里 `cn-hongkong`。
3. 容器内可见至少 2 vCPU，内存达到 4 GiB 规格的现有 90% 容差；128 MiB 内存触碰、16 MiB 同步读写、10,000 行 PostgreSQL dump/drop/restore 全部成功。
4. 三次 HTTPS health 全部成功，三次 SSE 都收到完整的三个事件。
5. GitHub Releases、arXiv API、DeepSeek API、Kimi API、GitHub OAuth 端点的固定探针在每次尝试中全部可达。
6. workload、PostgreSQL 和协议 SHA 与测试前冻结值完全一致；token 不写入结果；工作负载只对固定大陆观察者开放。

当前探针的 20 秒请求超时只是单次操作失败边界；现有 comparator **没有最大 Network latency、最大 SSE 首事件延迟、最低磁盘吞吐或最长数据库恢复时间的质量阈值**。它只在三个候选全部满足布尔门槛后，按 Network 中位数、SSE 首事件中位数、CPU 时间、月费等顺序排序。因此，单节点“质量尚可”不能仅写成主观判断，必须另行冻结建议阈值。

### 建议性质量阈值：决定采用还是继续测试

以下是本项目在没有三家相对排名时采用的产品决策线，不是云厂商 SLA，也不是当前 comparator 自带标准：

- 固定大陆观察者的三次 HTTPS health：中位数 **不高于 250 ms**，任一次 **不高于 500 ms**。
- 三次 SSE 首事件：中位数 **不高于 500 ms**，每次流均完整结束且无重连。
- 所有外连探针单次均在现有 **20 秒**超时内完成；任何固定端点三次中出现一次失败，就继续测试下一家。
- 在一个工作日白天和一个本地晚高峰各重复一轮 qualification；两轮均过硬门槛，且上述 latency 线都满足，才称“pilot 合格”。这是为了暴露官方已提示的资源争抢和跨境链路波动，两个结果文件应分别保留，不能冒充三云 comparator 输入。
- 如硬门槛全部通过但只超出建议 latency 线，保留节点至第二个时段复测；复测仍超线则继续下一家。不要因一次偶发慢请求立即购买第二台，也不要因一次低延迟立即停止观察。

若实际产品更重视交互响应，可以在购买前把 250/500 ms 调得更严格；关键是必须在看到结果前冻结且随后不变。

## 首个腾讯节点的执行顺序

1. 在购买前用腾讯官方工单确认香港 Lighthouse 支持 workload 使用的 `instance-id` 和 `placement/region` 元数据路径，并能从 Docker 容器访问；同时在控制台确认该实名主体、该套餐类型是否具备首次 5 天无理由退还资格。
2. 选择中国香港、Linux、锐驰型、2 vCPU / 4 GB / 60 GB SSD、200 Mbps、不限流量、1 个月、关闭自动续费。选择纯 Ubuntu 22.04 LTS x86_64 系统镜像，不选应用镜像，不附加数据盘、快照套餐、对象存储或安全增值服务。
3. 保存订单页的规格、最终含税金额、退款资格与自动续费状态；配置 SSH 公钥，不把私钥、账号凭据或临时 token 发到聊天、Issue 或仓库。
4. 实例创建后先在宿主机和容器中验证 metadata 返回非空实例 ID 与 `ap-hongkong`；失败则立即保留错误证据并停止后续性能结论，按退还资格处理实例。
5. 使用 Issue #7 已冻结的一份 workload 镜像和 PostgreSQL 镜像，不在节点上独立重建；部署 TLS，限制固定大陆观察者访问。
6. 在白天和晚高峰执行 qualification，保存原始 JSON、官方价格页面/账单截图和环境信息。
7. 两轮均达标：保留为 MVP 节点，但将 Issue #7 标记为“腾讯单节点 pilot 通过、三云比较未完成”。若主要败于大陆链路，导出必要证据后按退还规则处理实例，再按小时购买 AWS；若主要败于 CPU/磁盘持续性能，优先评估阿里同规格或非轻量型 CVM/ECS，不默认 AWS 会更好。

## 购买前检查清单

- [ ] 已冻结选择顺序、硬门槛、建议性质量阈值、观察者 ISP/城市、两个测试时段和最长试验时长。
- [ ] 腾讯官方已确认 Lighthouse 元数据路径与容器可达性；账号页面已确认首次 5 天退还资格或明确接受一个月试错成本。
- [ ] 下单页仍有中国香港锐驰型 2c4g、纯 Ubuntu 22.04 LTS x86_64 系统镜像；最终含税价格和附属资源价格仍在预算内。
- [ ] 已冻结 Git commit、protocol SHA-256、workload image SHA-256、PostgreSQL image SHA-256。
- [ ] 已准备固定大陆观察者、HTTPS 域名、DNS 控制权和 SSH 公钥；80/443、证书与反向代理不会缓冲 `/events`。
- [ ] 腾讯元数据在宿主机和容器内均返回实例 ID 与 `ap-hongkong`。
- [ ] 已准备结果文件命名，且不会把重复的单节点 JSON 复制成另外两家输入。
- [ ] 若转测腾讯，购买前已向官方确认 Lighthouse 元数据路径；若转测阿里，购买前已确认 SWAS 支持安全加固实例身份文档。
- [ ] 已设置结束日期和清理清单：实例、静态 IP、磁盘、快照、DNS 记录和临时 TLS/workload token。

## 解释边界

这份排序回答的是“预算只能先买一台时，哪一台最值得先验证”。它不声称腾讯的大陆网络一定优于 AWS 或阿里，也不把 200 Mbps 峰值、BGP 或厂商品牌当作真实质量证据。最终采用结论只适用于记录中的节点规格、镜像、观察者、ISP 路由、价格和测试日期；其中任一项发生实质变化都应重新 qualification。
