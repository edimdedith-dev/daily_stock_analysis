#!/usr/bin/env python3
"""
A股双名单综合筛选脚本 v5（T+2埋伏策略版）
核心逻辑：今天分析 → 明天买入（回调时）→ 后天卖出

收盘后运行（推荐）：
  - 涨停名单用今天数据（最准确）
  - 主力资金/龙虎榜用今天数据

盘中运行：
  - 跳过涨停名单（数据不完整）
  - 主力资金/龙虎榜用昨天数据（T-1）
  - 结果仅供参考
"""

import os
import sys
import tushare as ts
import pandas as pd
from datetime import datetime, timedelta
import time

TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
MAX_STOCKS = 20
MIN_MARKET_CAP = 10
MAX_DAYS = 6

def get_today():
    return datetime.now().strftime('%Y%m%d')

def get_yesterday():
    """获取上一个交易日（简单用昨天，非交易日Tushare会自动处理）"""
    yesterday = datetime.now() - timedelta(days=1)
    # 如果昨天是周末，往前找
    while yesterday.weekday() >= 5:
        yesterday -= timedelta(days=1)
    return yesterday.strftime('%Y%m%d')

def is_after_close():
    """判断现在是否是收盘后（北京时间15:00后）UTC 7:00后"""
    now = datetime.utcnow()
    total_minutes = now.hour * 60 + now.minute
    return total_minutes >= 420  # UTC 7:00 = 北京时间15:00

def init_tushare():
    if not TUSHARE_TOKEN:
        print("❌ 未设置TUSHARE_TOKEN")
        sys.exit(1)
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()
    print("✅ Tushare初始化成功")
    return pro

# ============================================================
# 收盘后：涨停名单（今天数据）
# ============================================================
def fetch_limit_up(pro, trade_date):
    print(f"\n📡 获取{trade_date}涨停名单...")
    try:
        df = pro.limit_list_d(trade_date=trade_date, limit_type='U')
        if df is not None and not df.empty:
            print(f"✅ 涨停名单：{len(df)}只")
            df['source'] = 'limit_up'
            df['source_weight'] = 0.6
            return df
    except Exception as e:
        print(f"⚠️ limit_list_d失败: {e}")

    print("⚠️ 备用：日线数据筛选...")
    try:
        df = pro.daily(trade_date=trade_date)
        if df is not None and not df.empty:
            limit = df[df['pct_chg'] >= 9.9].copy()
            if not limit.empty:
                limit['source'] = 'limit_up'
                limit['source_weight'] = 0.6
                limit['days'] = 1
                print(f"✅ 日线备用：{len(limit)}只")
                return limit
    except Exception as e:
        print(f"❌ 日线备用失败: {e}")

    return pd.DataFrame()

# ============================================================
# 主力资金净流入
# ============================================================
def fetch_moneyflow_top(pro, trade_date):
    print(f"\n💰 获取{trade_date}主力资金净流入...")
    time.sleep(3)
    try:
        df = pro.moneyflow(
            trade_date=trade_date,
            fields='ts_code,buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount'
        )
        if df is not None and not df.empty:
            df['net_mf_amount'] = pd.to_numeric(df['net_mf_amount'], errors='coerce').fillna(0)
            df = df[df['net_mf_amount'] > 0]
            df = df.sort_values('net_mf_amount', ascending=False).head(50)
            df['source'] = 'moneyflow'
            df['source_weight'] = 0.4
            print(f"✅ 主力流入：{len(df)}只")
            return df
    except Exception as e:
        print(f"❌ 主力资金失败: {e}")
    return pd.DataFrame()

# ============================================================
# 龙虎榜
# ============================================================
def fetch_top_list(pro, trade_date):
    print(f"\n🏆 获取{trade_date}龙虎榜...")
    time.sleep(2)
    try:
        df = pro.top_list(
            trade_date=trade_date,
            fields='ts_code,name,close,pct_chg,turnover_rate,buy_amount,sell_amount,net_amount,reason'
        )
        if df is not None and not df.empty:
            df['net_amount'] = pd.to_numeric(df['net_amount'], errors='coerce').fillna(0)
            df = df[df['net_amount'] > 0]
            df = df.sort_values('net_amount', ascending=False)
            df = df.drop_duplicates(subset='ts_code', keep='first')
            print(f"✅ 龙虎榜：{len(df)}只")
            return df
    except Exception as e:
        print(f"⚠️ 龙虎榜失败: {e}")
    return pd.DataFrame()

# ============================================================
# 基本数据
# ============================================================
def fetch_basics(pro, trade_date, stock_list):
    print(f"\n📊 获取{len(stock_list)}只基本数据...")
    try:
        codes = ','.join(stock_list[:100])
        df = pro.daily_basic(
            trade_date=trade_date,
            ts_code=codes,
            fields='ts_code,turnover_rate,volume_ratio,circ_mv,pe,pb'
        )
        if df is not None and not df.empty:
            print(f"✅ 基本数据：{len(df)}只")
            return df
    except Exception as e:
        print(f"⚠️ 基本数据失败: {e}")
    return pd.DataFrame()

# ============================================================
# 七因子评分（T+2埋伏策略）
# ============================================================
def score_stock(row, basics_df, source):
    ts_code = row.get('ts_code', '')
    score = 0
    basic = basics_df[basics_df['ts_code'] == ts_code] if not basics_df.empty else pd.DataFrame()

    # 因子A：涨停质量（15分）
    pct_chg = float(row.get('pct_chg', 0) or 0)
    if pct_chg >= 19.5:
        a = 15
    elif pct_chg >= 9.9:
        a = 10
    elif pct_chg >= 7:
        a = 6
    elif pct_chg >= 5:
        a = 4
    else:
        a = 0
    score += a

    # 因子B：连板动量（30分）T+2核心
    days = int(row.get('days', 1) or 1)
    if days == 3:
        b = 30
    elif days == 2:
        b = 28
    elif days == 4:
        b = 20
    elif days == 1:
        b = 8
    elif days == 5:
        b = 10
    else:
        b = 5
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

    # 因子D：量比（12分）
    if not basic.empty:
        vol_ratio = float(basic.iloc[0].get('volume_ratio', 1) or 1)
        if vol_ratio >= 3:
            d = 12
        elif vol_ratio >= 2:
            d = 10
        elif vol_ratio >= 1.5:
            d = 7
        else:
            d = 4
    else:
        d = 6
    score += d

    # 因子E：题材（10分）基础分
    score += 7

    # 因子F：流动性（8分）
    if not basic.empty:
        circ_mv = float(basic.iloc[0].get('circ_mv', 0) or 0) / 10000
        if 20 <= circ_mv <= 100:
            f = 8
        elif 100 < circ_mv <= 300:
            f = 5
        elif 10 <= circ_mv < 20:
            f = 3
        elif circ_mv > 300:
            f = 2
        else:
            f = 0
        score += f
    else:
        score += 4

    # 因子G：技术形态（5分）
    score += 3

    # 龙虎榜加分
    if row.get('in_top_list', False):
        score += 10

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
    print("=" * 60)
    print("🚀 A股双名单筛选系统 v5（T+2埋伏策略）")
    print("策略：今天分析 → 明天买入回调 → 后天卖出")
    print("=" * 60)

    today = get_today()
    yesterday = get_yesterday()
    after_close = is_after_close()

    print(f"📅 今天: {today} | 昨天: {yesterday}")
    print(f"🕐 当前UTC: {datetime.utcnow().strftime('%H:%M')}")

    if after_close:
        print("📊 收盘后模式：用今天数据（最准确）")
        limit_date = today
        flow_date = today
    else:
        print("📊 盘中模式：跳过涨停名单，用昨天资金数据（T-1）")
        print("⚠️ 建议收盘后（北京时间15:00后）重新运行获取最终结果")
        limit_date = None  # 盘中跳过涨停名单
        flow_date = yesterday

    pro = init_tushare()

    # 获取数据
    limit_up_df = pd.DataFrame()
    if limit_date:
        limit_up_df = fetch_limit_up(pro, limit_date)

    moneyflow_df = fetch_moneyflow_top(pro, flow_date)
    top_list_df = fetch_top_list(pro, flow_date)

    # 检查数据
    if limit_up_df.empty and moneyflow_df.empty:
        print("\n❌ 今日无有效数据")
        if not after_close:
            print("盘中模式下主力资金和龙虎榜数据也为空，请检查Tushare积分或网络")
        with open('screened_stocks.txt', 'w') as f:
            f.write('')
        sys.exit(0)

    # 合并股票池
    all_stocks = {}

    if not limit_up_df.empty and 'ts_code' in limit_up_df.columns:
        for _, row in limit_up_df.iterrows():
            code = row['ts_code']
            all_stocks[code] = dict(row)
            all_stocks[code]['source'] = 'limit_up'
            all_stocks[code]['source_weight'] = 0.6
            all_stocks[code]['in_top_list'] = False

    if not moneyflow_df.empty and 'ts_code' in moneyflow_df.columns:
        for _, row in moneyflow_df.iterrows():
            code = row['ts_code']
            if code in all_stocks:
                all_stocks[code]['net_mf_amount'] = row.get('net_mf_amount', 0)
                all_stocks[code]['source'] = 'both'
                all_stocks[code]['source_weight'] = 1.0
            else:
                all_stocks[code] = dict(row)
                all_stocks[code]['source'] = 'moneyflow'
                all_stocks[code]['source_weight'] = 0.4
                all_stocks[code]['in_top_list'] = False

    if not top_list_df.empty and 'ts_code' in top_list_df.columns:
        for code in top_list_df['ts_code'].tolist():
            if code in all_stocks:
                all_stocks[code]['in_top_list'] = True

    print(f"\n📊 合并后共{len(all_stocks)}只候选")
    both = sum(1 for v in all_stocks.values() if v.get('source') == 'both')
    tl = sum(1 for v in all_stocks.values() if v.get('in_top_list'))
    d2 = sum(1 for v in all_stocks.values() if int(v.get('days', 1) or 1) == 2)
    d3 = sum(1 for v in all_stocks.values() if int(v.get('days', 1) or 1) == 3)
    print(f"   ⭐ 双榜股: {both}只 | 🏆 龙虎榜: {tl}只")
    print(f"   🔥 2连板: {d2}只 | 💥 3连板: {d3}只（T+2最佳目标）")

    # 获取基本数据（用flow_date，盘中用昨天）
    stock_list = list(all_stocks.keys())
    basics_df = fetch_basics(pro, flow_date, stock_list)

    # 评分过滤
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
            'source': row.get('source', 'limit_up'),
            'in_top_list': row.get('in_top_list', False)
        })

    print(f"\n⛔ 一票否决淘汰: {rejected}只")

    results.sort(key=lambda x: x['score'], reverse=True)
    top20 = results[:MAX_STOCKS]

    print("\n" + "=" * 60)
    if after_close:
        print(f"🏆 T+2埋伏策略 Top{len(top20)}（收盘后完整数据）")
    else:
        print(f"🏆 T+2埋伏策略 Top{len(top20)}（盘中参考，基于昨日资金数据）")
    print("=" * 60)

    icons = {'limit_up': '🔴涨停', 'moneyflow': '💰流入', 'both': '⭐双榜'}
    codes_out = []

    for i, item in enumerate(top20, 1):
        code = item['ts_code']
        codes_out.append(code)
        src = icons.get(item['source'], '📊')
        tl_mark = '🏆' if item['in_top_list'] else '  '
        days_mark = f"✨{item['days']}板" if item['days'] in [2, 3] else f"{item['days']}板"
        print(f"  {i:2d}. {code} | 评分:{item['score']:3d} | {src} {tl_mark} | {days_mark} | {item['pct_chg']:.1f}%")

    result_str = ','.join(codes_out)
    print(f"\n✅ STOCK_LIST={result_str}")

    with open('screened_stocks.txt', 'w') as f:
        f.write(result_str)

    print(f"\n📝 已写入 screened_stocks.txt")
    if not after_close:
        print("⚠️ 盘中结果仅供参考，收盘后重新运行获取最终结果")
    print("=" * 60)

if __name__ == '__main__':
    main()
