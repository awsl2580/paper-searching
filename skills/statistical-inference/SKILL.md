---
name: statistical-inference
title: 统计推断与效应量
description: 选对检验方法、报出效应量与置信区间、避免 p 值误用；用 MCP 统计工具真算而不是估
keywords: 统计, 显著性, 检验, p值, 相关, 方差, 回归, 差异, 效应量
roles: computationalist, critic
tools: stats_describe, stats_ttest, stats_mannwhitney, stats_anova, stats_correlation, stats_regression, stats_chi_square
---

## 什么时候用
要判断"两组有没有差异""两个量是否相关""这个结论站不站得住"时。

## 方法选择（先看数据形状，再选检验）
| 场景 | 检验 | 工具 |
|------|------|------|
| 两组连续变量、近似正态、方差相近 | Student t / Welch t | `stats_ttest` |
| 两组连续变量、有小样本或离群点、分布未知 | Mann-Whitney U | `stats_mannwhitney` |
| 三组及以上 | 单因素 ANOVA | `stats_anova` |
| 两个连续变量 | Pearson / Spearman | `stats_correlation` |
| 分类变量列联表 | 卡方 | `stats_chi_square` |
| 一个自变量预测因变量 | 线性回归 | `stats_regression` |

## 步骤
1. 先用 `stats_describe` 看 n、均值、标准差、极值 —— **先看数再选检验**，
   不要一上来就跑 t 检验。
2. 调用对应工具，把 t/F/r、df、**p**、效应量（Cohen's d / η² / r²）一起报出来。
3. 解读三件事，缺一不可：
   - 显著性（p 与 α=0.05 比较）；
   - 效应量（小 0.2 / 中 0.5 / 大 0.8 是经验阈值，要说明这是经验值）；
   - 样本量（n 太小的时候"不显著"说明不了任何事，只能说"证据不足"）。
4. 多重比较提醒：一次报 5 个以上 p 值时，要说明没有做校正，结论只作探索性证据。

## 注意
- **p 大不等于没差异**，只等于"没检出来"；n 小的时候尤其要这么写。
- **p 小不等于效应大**；n 很大时 0.01 的差异也能显著。
- 相关系数不是因果：r=0.9 只能写"共变"，不能写"导致"。
- 不要凭肉眼从图表里读数再拿去检验；能从数据算就用工具算。
- 结果里 `degenerate: true` 表示数据退化了（全常量/样本不足），必须如实报告。

## 验收
- 每个统计结论都有"检验名 + 统计量 + p + 效应量 + n"；
- 检验方法与数据类型匹配（分类变量不许用 t 检验）；
- 引用的数字来自工具返回，不是模型自己算的。
