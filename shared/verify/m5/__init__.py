# -*- coding: utf-8 -*-
"""M5 判决集 · 机器判据与统计运行器（子模块包）。

本包只被 `shared/verify/m5_judge.py` 使用；入口与 CLI 全部在 `m5_judge.py`。

模块划分：
  * ``contract``  —— 输入契约（用例集 / 答案 JSONL）与 fail-closed 校验
  * ``judge``     —— 逐用例判据（根因/层/路径/对象/证据/虚假归因/abstain）
  * ``stats``     —— 纯标准库统计（McNemar 精确、Cohen's h、Wilcoxon 符号秩、
                     Cliff's delta、配对 bootstrap CI、Bonferroni）与样本量纪律
  * ``reporting`` —— report.md 渲染（分层表 / 分族表 / Bonferroni 表 / 敏感性表）
  * ``selftest``  —— 合成夹具 + 反作弊负控（①–⑥）+ G6 区分度校验

★ 命名消歧（R-35）：本包中的 ``C1``–``C6`` 一律指 **docs/06 §1.1 的用例族**，
  与 docs/00 §3.1 的项目级架构约束 ``C1``–``C6``、M4 期契约门禁 ``M4-C1``…``M4-C9``
  **同名不同义** ⇒ 报告与输出中一律写作 ``族C1``…``族C6``。
"""

__all__ = ["contract", "judge", "stats", "reporting", "selftest"]

PKG_VERSION = "m5-judge 1.0"
