#!/usr/bin/env python3
"""
A股双名单综合筛选脚本 v3（Tushare专业版）
双名单策略：
1. 涨停名单（今日涨停）→ 短线确定性强，权重60%
2. 主力净流入名单（大资金买入但未必涨停）→ 提前埋伏，权重40%
两个名单合并去重，七因子综合评分，取Top20
"""

import os
import sys
import tushare as ts
import pandas as pd
from datetime import datetime
import time

# ===== 配置 =====
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
MAX_STOCKS = 20
MIN_MARKET_CAP = 10    # 最小流通市值（亿元）
MAX_DAYS = 7           # 连板超过此数淘汰

def get_today():
    return datetime.now().strftime('%Y%m%d')

def init_tushare():
    if not TUSHARE_TOKEN:
        print("❌ 未设置TUSHARE_TOKEN")
        sys.exit(1)
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()
    print("✅ Tushare初始化成功")
    return pro

# ============================================================
# 第一名单：涨停股票（Tushare limit_list_d接口）
# ============================================================
def fetch_limit_up(pro, trade_date):
    print(f"\n📡 获取{trade_date}涨停名单（Tushare）...")
    try:
        df = pro.limit_list_d(trade_date=trade_date, limit_type='U')
        if df is not None and not df.empty:
            print(f"✅ 涨停名单获取成功：{len(df)}只")
            df['source'] = 'limit_up'
            df['source_weight'] = 0.6
            return df
    except Exception as e:
        print(f"⚠️ limit_list_d失败: {e}")

    # 备用：从日线数据筛选
    print("⚠️ 尝试备用方案：从日线数据筛选...")
    try:
        df = pro.daily(trade_date=trade_date)
        if df is not None and not df.empty:
            limit = df[df['pct_chg'] >= 9.9].copy()
            limit['source'] = 'limit_up'
            limit['source_weight'] = 0.6
            limit['days'] = 1
            print(f"✅ 日线备用方案成功：{len(limit)}只")
            return limit
    except Exception as e:
        print(f"❌ 备用方案也失败: {e}")

    return pd.DataFrame()

# ============================================================
# 第二名单：主力资金净流入（Tushare moneyflow接口）
# ============================================================
def fetch_moneyflow_top(pro, trade_date):
    print(f"\n💰 获取{trade_date}主力资金净流入名单（Tushare）...")
    try:
        # 获取全市场资金流向，按净流入排序取Top50
        df = pro.moneyflow(
            trade_date=trade_date,
            fields='ts_code,buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount'
        )
        if df is not None and not df.empty:
            df['net_mf_amount'] = pd.to_numeric(df['net_mf_amount'], errors='coerce').fillna(0)
            # 只取净流入为正的
            df = df[df['net_mf_amount'] > 0]
            # 按净流入排序
            df = df.sort_values('net_mf_amount', ascending=False).head(50)
            df['source'] = 'moneyflow'
            df['source_weight'] = 0.4
            print(f"✅ 主力流入名单获取成功：{len(df)}只")
            return df
    except Exception as e:
        print(f"❌ 主力资金流向获取失败: {e}")
    return pd.DataFrame()

# ============================================================
# 获取个股基本数据（市值、换手率等）
# ============================================================
def fetch_basics(pro, trade_date, stock_list):
    print(f"\n📊 获取{len(stock_list)}只股票基本数据...")
    try:
        codes = ','.join(stock_list[:100])
        df = pro.daily_basic(
            trade_date=trade_date,
            ts_code=codes,
            fields='ts_code,turnover_rate,volume_ratio,circ_mv,pe,pb'
        )
        if df is not None and not df.empty:
            print(f"✅ 基本数据获取成功：{len(df)}只")
            return df
    except Exception as e:
        print(f"⚠️ 基本数据获取失败: {e}")
    return pd.DataFrame()

# ============================================================
# 七因子评分
# ============================================================
def score_stock(row, basics_df, source):
    ts_code = row.get('ts_code', '')
    score = 0

    # 获取基本数据
    basic = basics_df[basics_df['ts_code'] == ts_code] if not basics_df.empty else pd.DataFrame()

    # 因子A：涨停质量（22分）
    pct_chg = float(row.get('pct_chg', 0) or 0)
    if pct_chg >= 19.5:
        a = 22  # 20CM涨停
    elif pct_chg >= 9.9:
        a = 16  # 正常涨停
    elif pct_chg >= 7:
        a = 10
    elif pct_chg >= 5:
        a = 6
    elif pct_chg >= 3:
        a = 3
    else:
        a = 0
    score += a

    # 因子B：连板动量（18分）
    days = int(row.get('days', 1) or 1)
    if days >= 4:
        b = 18
    elif days == 3:
        b = 14
    elif days == 2:
        b = 10
    else:
        b = 6
    score += b

    # 因子C：资金流向（20分）
    if source == 'limit_up':
        fd_amount = float(row.get('fd_amount', 0) or 0)
        if fd_amount > 50000:
            c = 18
        elif fd_amount > 20000:
            c = 14
        elif fd_amount > 5000:
            c = 10
        else:
            c = 8
    else:
        net_mf = float(row.get('net_mf_amount', 0) or 0)
        if net_mf > 50000:
            c = 20
        elif net_mf > 20000:
            c = 16
        elif net_mf > 5000:
            c = 12
        elif net_mf > 1000:
            c = 8
        else:
            c = 4
    score += c

    # 因子D：情绪竞价（15分）- 用量比替代
    if not basic.empty:
        vol_ratio = float(basic.iloc[0].get('volume_ratio', 1) or 1)
        if vol_ratio >= 3:
            d = 15
        elif vol_ratio >= 2:
            d = 12
        elif vol_ratio >= 1.5:
            d = 8
        else:
            d = 4
    else:
        d = 7
    score += d

    # 因子E：题材板块（12分）- 基础分
    score += 8

    # 因子F：流动性（8分）
    if not basic.empty:
        circ_mv = float(basic.iloc[0].get('circ_mv', 0) or 0) / 10000  # 万元转亿元
        turnover = float(basic.iloc[0].get('turnover_rate', 0) or 0)
        if 20 <= circ_mv <= 50:
            f = 8
        elif 50 < circ_mv <= 200:
            f = 5
        elif 10 <= circ_mv < 20:
            f = 3
        elif circ_mv > 200:
            f = 2
        else:
            f = 0
        score += f
    else:
        score += 4

    # 因子G：技术形态（5分）- 基础分
    score += 3

    # 来源权重加成
    weight = float(row.get('source_weight', 0.5))
    final = round(score * weight + score * (1 - weight) * 0.8)
    return final

# ============================================================
# 一票否决
# ============================================================
def is_rejected(ts_code, days, basics_df):
    if 'ST' in ts_code.upper():
        return "ST股"
    if days > MAX_DAYS:
        return f"连板{days}板超上限"
    if not basics_df.empty:
        basic = basics_df[basics_df['ts_code'] == ts_code]
        if not basic.empty:
            circ_mv = float(basic.iloc[0].get('circ_mv', 0) or 0) / 10000
            if 0 < circ_mv < MIN_MARKET_CAP:
                return f"市值{circ_mv:.1f}亿过小"
            turnover = float(basic.iloc[0].get('turnover_rate', 0) or 0)
            if 0 < turnover < 2:
                return f"换手率{turnover:.1f}%过低"
    return None

# ============================================================
# 主程序
# ============================================================
def main():
    print("=" * 55)
    print("🚀 A股双名单综合筛选系统 v3（Tushare专业版）")
    print("=" * 55)

    trade_date = get_today()
    print(f"📅 交易日期: {trade_date}")

    pro = init_tushare()

    # 1. 获取两个名单
    limit_up_df = fetch_limit_up(pro, trade_date)
    time.sleep(1)
    moneyflow_df = fetch_moneyflow_top(pro, trade_date)

    # 2. 合并股票池
    all_stocks = {}

    if not limit_up_df.empty and 'ts_code' in limit_up_df.columns:
        for _, row in limit_up_df.iterrows():
            code = row['ts_code']
            all_stocks[code] = dict(row)
            all_stocks[code]['source'] = 'limit_up'
            all_stocks[code]['source_weight'] = 0.6

    if not moneyflow_df.empty and 'ts_code' in moneyflow_df.columns:
        for _, row in moneyflow_df.iterrows():
            code = row['ts_code']
            if code in all_stocks:
                # 两个名单都有，最强信号
                all_stocks[code]['net_mf_amount'] = row.get('net_mf_amount', 0)
                all_stocks[code]['source'] = 'both'
                all_stocks[code]['source_weight'] = 1.0
            else:
                all_stocks[code] = dict(row)
                all_stocks[code]['source'] = 'moneyflow'
                all_stocks[code]['source_weight'] = 0.4

    print(f"\n📊 合并后共{len(all_stocks)}只候选")
    both_count = sum(1 for v in all_stocks.values() if v.get('source') == 'both')
    print(f"   ⭐ 两个名单都有（最强信号）: {both_count}只")

    # 3. 获取基本数据
    time.sleep(1)
    stock_list = list(all_stocks.keys())
    basics_df = fetch_basics(pro, trade_date, stock_list)

    # 4. 评分+过滤
    results = []
    rejected = 0

    for code, row in all_stocks.items():
        days = int(row.get('days', 1) or 1)
        reject = is_rejected(code, days, basics_df)
        if reject:
            rejected += 1
            continue
        score = score_stock(row, basics_df, row.get('source', 'limit_up'))
        results.append({
            'ts_code': code,
            'score': score,
            'days': days,
            'pct_chg': row.get('pct_chg', 0),
            'source': row.get('source', 'limit_up')
        })

    print(f"\n⛔ 一票否决淘汰: {rejected}只")

    # 5. 排序取Top20
    results.sort(key=lambda x: x['score'], reverse=True)
    top20 = results[:MAX_STOCKS]

    # 6. 输出
    print("\n" + "=" * 55)
    print(f"🏆 综合评分 Top{len(top20)}")
    print("=" * 55)

    icons = {'limit_up': '🔴涨停', 'moneyflow': '💰流入', 'both': '⭐双榜'}
    codes_out = []

    for i, item in enumerate(top20, 1):
        code = item['ts_code']
        codes_out.append(code)
        src = icons.get(item['source'], '📊')
        print(f"  {i:2d}. {code} | 评分:{item['score']:3d} | {src} | {item['days']}板 | {item['pct_chg']:.1f}%")

    result_str = ','.join(codes_out)
    print(f"\n✅ STOCK_LIST={result_str}")

    with open('screened_stocks.txt', 'w') as f:
        f.write(result_str)

    print(f"\n📝 已写入 screened_stocks.txt")
    print("=" * 55)

if __name__ == '__main__':
    main()
