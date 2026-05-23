# SKILLS.md — 美股量化分析与智能投资系统

## 一、缠论分析能力（核心，权重 50%）

### 缠论五元素实现规范
| 元素 | 文件 | 实现要点 |
|------|------|---------|
| **分型** | `signals/chan/fractal.py` | 顶分型（中间 K 高于左右）/ 底分型（中间 K 低于左右）；包含关系处理（同向合并，取高高/低低） |
| **笔** | `signals/chan/stroke.py` | 顶→底或底→顶，至少 5 根独立 K（含分型 K），方向唯一，不重叠 |
| **线段** | `signals/chan/segment.py` | 3 笔构成 1 线段；线段破坏 = 特征序列分型 |
| **中枢** | `signals/chan/pivot.py` | 3 笔（或 3 线段）重叠区间 ZG/ZD，中枢扩展/延伸/升级规则 |
| **买卖点** | `signals/chan/trade_point.py` | 1买=背驰底 / 2买=中枢回调不破底 / 3买=中枢上方回试 |

### 多级别共振规则
```python
# multi_level.py 核心逻辑
levels = ["1d", "60min", "30min"]
resonance_count = sum(1 for lvl in levels if chan_signal[lvl].buy_point_type is not None)
# 3 → 重仓 / 2 → 标准仓 / 1 → 试仓 / 0 → 空仓
```

### ChanSignalResult 输出契约
```python
@dataclass
class ChanSignalResult:
    ticker: str
    timestamp: pd.Timestamp
    buy_point_type: Optional[str]   # "1buy" | "2buy" | "3buy" | None
    sell_point_type: Optional[str]  # "1sell" | "2sell" | "3sell" | None
    level_resonance: int            # 共振级别数 0–3
    current_pivot: Optional[Dict]   # {"ZG": float, "ZD": float, "level": str}
    last_stroke_direction: str      # "up" | "down"
    score: float                    # -1~1
    confidence: float               # 0~1
    reasoning: str                  # 自然语言解释，用于报告
```

### 防前视规则（强制）
- 分型/笔/中枢必须在 K 线**完成后**才确认，不预测进行中结构
- 多级别数据：日线用收盘价；60min/30min 仅取 t-1 完整周期

---

## 二、技术信号能力（权重 30%）

### 技术因子清单（精选 20 个，避免冗余）
| 类别 | 因子 | 计算规范 |
|------|------|---------|
| 趋势 | SMA20 / SMA60 / SMA200 | 简单移动均线，信号：价格站上/站下 |
| 趋势 | EMA12 / EMA26 | 指数移动均线 |
| 趋势 | ADX14 | 趋势强度，> 25 视为趋势明确 |
| 动量 | MACD(12,26,9) | DIF / DEA / 柱状 |
| 动量 | RSI14 | 超买 > 70 / 超卖 < 30 |
| 动量 | KAMA | 适应性移动均线，择时使用 |
| 动量 | ROC20 | 20 日变化率 |
| 波动 | ATR14 | 真实波动幅度，止损定价基准 |
| 波动 | BB(20,2) | 布林带，squeeze 检测 |
| 成交量 | OBV | 能量潮，配合价格趋势确认 |
| 成交量 | VWMA20 | 量价加权均线 |

### TechnicalSignalResult 输出契约
```python
@dataclass
class TechnicalSignalResult:
    ticker: str
    indicators: Dict[str, float]  # 所有因子值
    trend: str                    # "up" | "down" | "neutral"
    momentum_score: float         # -1~1
    score: float                  # -1~1，综合技术得分
    reasoning: str
```

---

## 三、宏观信号能力（权重 20%）

### FRED 宏观序列清单
| FRED 代码 | 含义 | 更新频率 |
|----------|------|---------|
| `FEDFUNDS` | 联邦基金利率 | 月 |
| `DGS10` | 10年期国债收益率 | 日 |
| `DGS2` | 2年期国债收益率（倒挂信号） | 日 |
| `CPIAUCSL` | CPI（YoY） | 月 |
| `UNRATE` | 失业率 | 月 |
| `T10YIE` | 10年盈亏平衡通胀率 | 日 |

### VIX 四档制度（原创设计）
```python
def classify_vix_regime(vix: float) -> dict:
    if vix < 15:   return {"regime": "calm",    "position_limit": 1.0}
    if vix < 25:   return {"regime": "neutral",  "position_limit": 0.7}
    if vix < 35:   return {"regime": "tense",    "position_limit": 0.4}
    return             {"regime": "panic",    "position_limit": 0.0}
```

### 3 桶相对强度
```python
BUCKETS = {
    "mega_tech":  ["GOOGL","AAPL","NVDA","MSFT","META"],
    "consumer":   ["AMZN","TSLA"],
    "hardware":   ["SNDK","VRT"],
}
# 桶强度 = IR = 桶均收益 / 桶均收益标准差（相对 QQQ 超额）
# 桶内排名 = 个股 20 日动量 Z-score
```

---

## 四、决策层能力

### 三维共振打分（原创）
```python
final_score = 0.5 * chan_score + 0.3 * tech_score + 0.2 * macro_score
# 同向强化：若三维同号，bonus +0.1
# 背离惩罚：若缠论与技术反号，score *= 0.5
```

### 5 档评级映射（TradingAgents 启发）
| final_score | 评级 | 建议仓位 |
|------------|------|---------|
| > 0.6 | **Buy** | position_limit × 100% |
| 0.3–0.6 | **Overweight** | position_limit × 70% |
| -0.3–0.3 | **Hold** | position_limit × 0% (持仓不动) |
| -0.6–-0.3 | **Underweight** | 减半仓 |
| < -0.6 | **Sell** | 清仓 |

### 风控三级（FinRL-X 启发）
```python
# 订单级：单笔最大金额
# 组合级：单次换手率上限 50%
# 策略级：止损 = 入场价 - 2×ATR14；追踪止损 = 最高价 - 1.5×ATR14
```

---

## 五、数据工程能力

### DataSource Protocol（FinRL-X 启发）
```python
class DataSource(Protocol):
    def is_available(self) -> bool: ...
    def get_price(self, ticker, start, end) -> pd.DataFrame: ...
    def get_news(self, ticker, days) -> pd.DataFrame: ...
    def get_macro(self, series_id) -> pd.DataFrame: ...
```
优先级：yfinance（主）→ Alpha Vantage（财报备用）→ Finnhub（新闻补充）

### 无前视偏差（双重保障）
1. **财报特征**：datadate + 2 月延迟（FinRL-X ML_STOCK_SELECTION.md 标准）
2. **实时特征**：仅使用 close[t-1]，当日收盘后计算，隔日应用

### SQLite 缓存策略
- 日线价格：缓存 2 年，TTL 1 天
- 宏观数据：缓存 30 天，TTL 1 天（FRED 月度更新）
- 财报数据：缓存 90 天，TTL 7 天
- 新闻/情绪：缓存 7 天，不 TTL（历史新闻不变）

---

## 六、回测能力

### bt 库（FinRL-X 标准，不自研回测）
```python
import bt
strategy = bt.Strategy("chan_triple", [bt.algos.WeighTarget(weights_df)])
backtest = bt.Backtest(strategy, price_data, commissions=lambda q,p: 0.001*abs(q)*p)
result = bt.run(backtest)
result.display()  # 含 Sharpe / MaxDD / Calmar / 胜率
```

### 回测指标标准
- 年化收益率（Annualized Return）
- 夏普比率（Sharpe Ratio）≥ 1.0 为目标
- 最大回撤（MaxDD）≤ -25% 预警
- Calmar 比率（年化收益 / MaxDD）
- 胜率（Win Rate）

---

## 七、报告输出能力（financial-services 启发）

### 单股报告结构
```markdown
# {TICKER} 分析报告 — {日期}

## 📊 评级：{Buy/Hold/Sell}  得分：{score:.2f}
- 建议仓位：{position}%  入场区间：${low}–${high}
- 止损：${stop_loss}  目标：${take_profit}

## 🌀 缠论分析
{chan_signal.reasoning}

## 📈 技术面概要
{technical_signal.reasoning}

## 🌍 宏观环境
{macro_signal.reasoning}

## ⚠️ 风险提示
{risk_flags}
```

### 每日总览结构
- 宏观制度简报（VIX/利率/制度档位）
- 9 股评级排名表（Buy → Sell 排序）
- 桶强度对比（mega_tech vs consumer vs hardware）

---

## 八、启用 Skills
- **Context7** — 阅读及分析最新 GitHub 项目与库文档
- **brainstorming** — 策略方案多路径对比
- **code-review** — 因子/模型代码质量自检
- **document-skills** — 策略说明文档自动生成
- **diagram-generator** — 架构图/回测曲线/因子分布可视化
- **superpowers** — 复杂量化任务增强推理
