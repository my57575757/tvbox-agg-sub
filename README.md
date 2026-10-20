# tvbox-agg-sub

TVBox 多仓递归聚合：以 [juhe-subhub 的 merged.json](https://ofind.cn/juhe-subhub/merged.json) 为种子，递归展开其中所有多仓，去重合并为单个 TVBox 配置（sites/parses/lives），GitHub Actions 每天北京时间 00:17 自动重新生成并提交。

## 订阅地址

原始（直连）：

```
https://raw.githubusercontent.com/my57575757/tvbox-agg-sub/main/aggregate.json
```

国内网络打不开 raw 时走代理：

```
https://gh-proxy.com/https://raw.githubusercontent.com/my57575757/tvbox-agg-sub/main/aggregate.json
```

在 MBox/TVBox 的「订阅地址」里填上面任意一个即可。失败仓与每仓贡献站点数见每次构建的 stats artifact。

## 本地运行

```bash
python aggregate.py seed.json aggregate.json stats.txt
```

- 对 `//` 整行注释、尾逗号等非严格 JSON 做了容错
- 非 ASCII 域名走 IDNA，非 ASCII 路径 percent-encode
- gh-proxy 类代理 URL 自动回退直连
- 失效/反爬仓库跳过，不影响整体产物
