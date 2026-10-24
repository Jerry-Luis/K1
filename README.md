# 6指标估值模型 V3 — 交易系统

每周选股重算流水线。全A股 → 九条件粗筛 → DCF + 格林厄姆估值 → 六指标打分 → 硬门槛过滤 → 报告。

## 快速开始

```bash
bash run_screen.sh                  # 完整流程（约 30 秒）
bash run_screen.sh --limit 30       # 只跑前 30 只（调试）
bash run_screen.sh --dry-run        # 只粗筛，不做估值
bash run_screen.sh --skip-netcheck  # 跳过联网自检（离线排查用）
```

退出码：`0` 成功 ｜ `2` 流水线异常 ｜ `3` 前置自检失败（环境不满足）

## 产出

| 文件 | 说明 |
|---|---|
| `/workspace/报告/YYYY-MM-DD_选股筛选_估值模型.md` | 完整报告（概览/前5名/通过列表/全量排名/失败项） |
| `/workspace/报告/YYYY-MM-DD_选股筛选_估值模型.xlsx` | 5 个 sheet（概览/通过/全量/失败/剔除） |
| `/workspace/选股结果.json` | 供分时采集器读取，**只含通过硬门槛的标的** |
| `screen_pool2.md` | 粗筛池快照 |
| `logs/run_YYYYMMDD.log` | 运行日志 |

## 目录结构

```
交易系统/
├── run_screen.sh          # 入口：前置自检 + 编排
├── bootstrap.sh           # 持久化引导：缺失则从 git 远端恢复
├── requirements.txt
├── config/
│   ├── model_v3.md        # ★ 模型口径文档（唯一权威定义）
│   └── model_v3.json      # ★ 机器可读参数（权重/阈值/参数）
├── scripts/
│   ├── datasource.py      # 新浪全A快照 + 东财F10财务 + 腾讯行情
│   ├── model.py           # 粗筛/DCF/格林厄姆/分位打分/硬门槛
│   ├── pipeline.py        # 7 阶段编排
│   └── reporter.py        # md / xlsx / json 输出
└── screen_pool2.md        # 粗筛池（每次运行覆盖）
```

## 数据源说明

**iFinD 本月额度已耗尽**，改用免费公开接口，实测更快且更省：

| 用途 | 接口 | 调用量 |
|---|---|---|
| 全A快照（价/PE/PB/市值） | 新浪 `Market_Center.getHQNodeData` | 56 次（每页100只） |
| 三年财务（ROE/负债率/EPS/CAGR） | 东财 F10 `RPT_F10_FINANCE_MAINFINADATA` | 1 次/股 |
| 实时价交叉校验 | 腾讯 `qt.gtimg.cn` | 备用 |

对比原 iFinD 方案（每只 2 次请求 × 100 只 = 200 次，约 14 分钟）：
现方案 **156 次请求、约 26 秒**。且东财一次请求即可返回全部历史期，
无需拆分"财务三年"与"未来3年CAGR"两次调用。

> 如需切回 iFinD，改 `config/model_v3.json` 的 `datasource.primary`，
> 并在 `scripts/datasource.py` 增加 iFinD 适配函数即可，模型层无需改动。

## 持久化机制（重要）

**问题**：定时任务每次调度落在全新沙箱，`/workspace` 内容不保留。

**方案**：
1. 本目录已是 **git 仓库**，配置文件与代码全部入库（密钥/日志已 gitignore）
2. `run_screen.sh` 启动时调用 `bootstrap.sh` 自检，文件残缺则从远端 clone 恢复
3. 需配置远端仓库，否则仅依赖本地磁盘（重置即丢失）：

```bash
cd /workspace/交易系统
git remote add origin <私有仓库URL>
git push -u origin master
echo "<私有仓库URL>" > ~/.trade_system_remote
```

支持环境变量 `TRADE_GIT_REMOTE` 覆盖。`bootstrap.sh --force` 可强制覆盖本地。

## 前置自检（run_screen.sh 内置）

任一失败即打印原因并以 `3` 退出，**不会静默产出空报告**：

| # | 检查项 | 失败表现 |
|---|---|---|
| 0 | 持久化引导 | 列出缺失文件 + 远端提示 |
| 1 | Python 3.9+ 与依赖 | `缺少 Python 依赖: xxx` |
| 2 | 模型定义完整（json 含 weights） | `格式错误或缺少 weights` |
| 3 | 4 个核心脚本齐全 | `核心脚本缺失: xxx` |
| 4 | 输出目录可写 | `报告目录不可写` |
| 5 | git 仓库完整性 | `fsck` 告警（仅警告） |
| 6 | 数据源探活（全A≥3000只 + 财务可读） | `数据源不可达` / `全A快照异常` |

产物落盘后还会二次校验文件非空，缺失则退出码 1。

## 模型口径速查

| 项 | 值 |
|---|---|
| DCF 基准现金流 | 最新年报归母净利润 |
| 增长率 | 最近 3 年年报净利 CAGR，截断 [-10%, 30%] |
| 折现率 / 永续增长 | 10% / 2.5% |
| 格林厄姆 | `EPS(年报) × (8.5 + 2g) × 4.4 / 2.0` |
| 六指标权重 | DCF折价30% 格氏折价20% ROE15% 成长15% PE10% 稳健10% |
| 硬门槛 | DCF折价 ≥20% **且** 现价 < 格氏价值 |

完整定义与已知局限见 **`config/model_v3.md`**。改口径必须先改文档。

## 已知问题

1. **格林厄姆公式在低利率下失真**：`4.4/2.0 = 2.2` 倍放大，g=25% 时隐含 129 倍 PE。
   已加 5 倍熔断（价值/现价>5 判为不适用），代价是高成长股会被剔除。
2. **DCF 用净利替代自由现金流**，高估重资产行业（面板/航空/有色）。
3. 分位打分是池内相对值，硬门槛优先于总分。
