# CLAUDE.md — 美股量化分析与智能投资系统

## 项目目标
构建融合缠论信号、多因子选股、ML决策的美股量化投研系统，
覆盖选股→回测→风控→策略生成全流程，支持VS Code本地部署运行。

## 三层架构
- 底座层：数据获取（yfinance/Finnhub/Alpha Vantage/FRED）+ 数据清洗
- 信号层：缠论算法化（分型/笔/中枢/买卖点）+ 技术因子
- 决策层：多因子+ML模型 + 风控校验 + 仓位管理

## 技术栈
Python · Backtrader · VectorBT · Pandas · NumPy · Scikit-learn
LightGBM · PyTorch · Sub-agents · Context7 · Graphiti MCP

## 开发四原则
1. 先思考再编码：多方案对比，不确定时主动提问
2. 极简优先：不写超前冗余逻辑，冗长代码主动重构
3. 精准修改：仅改动需求指定代码，原有代码不随意重构
4. 目标导向：每步设定可核验标准，回测结果前后一致

## 输出规范
策略代码可运行 · 回测报告含绩效指标 · 因子模型有说明文档
所有策略禁止前视偏差 · 关键节点调用Graphiti存储优化记录

## Context7规则
涉及库文档、API用法、代码生成时自动调用Context7，无需显式指令。