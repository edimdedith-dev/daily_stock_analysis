#!/usr/bin/env python3
"""
A股双名单综合筛选脚本 v2
完全免费，使用AkShare数据源

双名单策略：
1. 涨停名单（今日涨停）→ 短线确定性强，权重60%
2. 主力净流入名单（大资金买入但未必涨停）→ 提前埋伏，权重40%
两个名单合并去重，七因子综合评分，取Top20
"""

import os
import sys
import pandas as pd
import akshare as ak
from datetime import datetime
import time

# ===== 配置 =====
MAX_STOCKS = 20       # 最多输出20只
MIN_MARKET_CAP = 10   # 最小流通市值（亿元）
MAX_MARKET_CAP = 500  # 最大流通市值（亿元）
MIN_TURNOVER = 2      # 最小换手率(%)
MAX_DAYS = 7          # 连板超过此数淘汰（高风险）

def get_today():
    return datetime.now().strftime('%Y%m%d')

def safe_fetch(func, desc, retries=3):
    """安全获取数据，失败自动重试"""
    for i in range(retries):
        try:
            result = func()
            if result is not None and not result.empty:
                print(f"✅ {desc} 获取成功（{len(result)}条）")
                return result
        except Exception as e:
            print(f"⚠️ {desc} 第{i+1}次失败: {str(e)[:80]}")
            time.sleep(2)
    print(f"❌ {desc} 获取失败，跳过")
    return pd.DataFrame()

# ============================================================
# 第一名单：涨停池（今日涨停股票）
# ============================================================
def fetch_limit_up_list():
    """获取今日涨停股票名单"""
    print("\n📡 获取今日涨停名单...")

    # 方法1：东财涨停池
    def try_em():
        df = ak.stock_zt_pool_em(date=get_today())
        return df

    # 方法2：强势股池
    def try_strong():
        df = ak.stock_zt_pool_strong_em(date=get_today())
        return df

    df = safe_fetch(try_em, "东财涨停池")

    if df.empty:
        df = safe_fetch(try_strong, "强势股池")

    if df.empty:
        print("⚠️ 涨停池获取失败，尝试从实时行情筛选...")
        return fetch_limit_up_from_realtime()

    # 标准化列名
    df = df.rename(columns={
        '代码': 'code',
        '名称': 'name',
        '涨跌幅': 'pct_chg',
        '连板数': 'days',
        '封单金额': 'limit_amount',
        '换手率': 'turnover',
        '流通市值': 'circ_mv',
        '总市值': 'total_mv',
        '涨停': 'is_limit'
    })

    # 添加来源标记
    df['source'] = 'limit_up'
    df['source_weight'] = 0.6  # 涨停名单权重60%

    print(f"📈 涨停名单共 {len(df)} 只")
    return df

def fetch_limit_up_from_realtime():
    """从实时行情筛选涨停股（备用）"""
    def try_fetch():
        df = ak.stock_zh_a_spot_em()
        limit = df[df['涨跌幅'] >= 9.9].copy()
        limit = limit.rename(columns={
            '代码': 'code',
            '名称': 'name',
            '涨跌幅': 'pct_chg',
            '换手率': 'turnover',
            '流通市值': 'circ_mv'
        })
        return limit

    df = safe_fetch(try_fetch, "实时行情涨停筛选")
    if not df.empty:
        df['source'] = 'limit_up'
        df['source_weight'] = 0.6
        df['days'] = 1
    return df

# ============================================================
# 第二名单：主力净流入（大资金买入但未必涨停）
# ============================================================
def fetch_moneyflow_list():
    """获取今日主力资金净流入排行"""
    print("\n💰 获取主力资金净流入名单...")

    def try_fetch():
        df = ak.stock_individual_fund_flow_rank(indicator="今日")
        return df

    df = safe_fetch(try_fetch, "主力资金流向排行")

    if df.empty:
        return pd.DataFrame()

    # 标准化列名
    col_map = {}
    for col in df.columns:
        if '代码' in col:
            col_map[col] = 'code'
        elif '名称' in col or '股票名' in col:
            col_map[col] = 'name'
        elif '净额' in col or '净流入' in col:
            col_map[col] = 'net_flow'
        elif '涨跌幅' in col or '涨跌' in col:
            col_map[col] = 'pct_chg'
        elif '换手' in col:
            col_map[col] = 'turnover'
    df = df.rename(columns=col_map)

    # 只取净流入为正的（大资金在买）
    if 'net_flow' in df.columns:
        df['net_flow'] = pd.to_numeric(df['net_flow'], errors='coerce').fillna(0)
        df = df[df['net_flow'] > 0]

    # 取前50名
    df = df.head(50)
    df['source'] = 'moneyflow'
    df['source_weight'] = 0.4  # 主力流入名单权重40%
    df['days'] = df.get('days', 1)

    print(f"💹 主力净流入名单共 {len(df)} 只")
    return df

# ============================================================
# 七因子评分
# ============================================================
def score_stock(row, source):
    """对单只股票七因子打分"""
    score = 0

    # 因子A：涨停质量（22分）
    pct_chg = float(row.get('pct_chg', 0) or 0)
    if pct_chg >= 19.5:      # 20CM涨停
        a_score = 22
    elif pct_chg >= 9.9:     # 正常涨停
        a_score = 16
    elif pct_chg >= 7:       # 强势但未涨停
        a_score = 10
    elif pct_chg >= 5:       # 中等强势
        a_score = 6
    elif pct_chg >= 3:       # 温和上涨
        a_score = 3
    else:
        a_score = 0
    score += a_score

    # 因子B：连板动量（18分）
    days = int(row.get('days', 1) or 1)
    if days >= 4:
        b_score = 18
    elif days == 3:
        b_score = 14
    elif days == 2:
        b_score = 10
    else:
        b_score = 6
    score += b_score

    # 因子C：资金流向（20分）
    net_flow = float(row.get('net_flow', 0) or 0)
    if source == 'limit_up':
        # 涨停股默认给中等资金分
        limit_amount = float(row.get('limit_amount', 0) or 0)
        if limit_amount > 50000:   # 封单>5亿
            c_score = 18
        elif limit_amount > 20000: # 封单>2亿
            c_score = 14
        elif limit_amount > 5000:  # 封单>5000万
            c_score = 10
        else:
            c_score = 8
    else:
        # 主力流入名单，用净流入金额评分
        if net_flow > 50000:   # >5亿
            c_score = 20
        elif net_flow > 20000: # >2亿
            c_score = 16
        elif net_flow > 5000:  # >5000万
            c_score = 12
        elif net_flow > 1000:  # >1000万
            c_score = 8
        else:
            c_score = 4
    score += c_score

    # 因子D：情绪竞价（15分）- 用换手率替代
    turnover = float(row.get('turnover', 0) or 0)
    if 5 <= turnover <= 15:
        d_score = 15
    elif 3 <= turnover < 5 or 15 < turnover <= 25:
        d_score = 10
    elif 2 <= turnover < 3:
        d_score = 6
    else:
        d_score = 3
    score += d_score

    # 因子E：题材板块（12分）- 基础分
    score += 8

    # 因子F：流动性（8分）
    circ_mv = float(row.get('circ_mv', 0) or 0)
    # AkShare的流通市值单位是元，转换为亿元
    if circ_mv > 1e8:
        circ_mv = circ_mv / 1e8
    elif circ_mv > 1000:
        circ_mv = circ_mv / 10000  # 万元转亿元

    if 20 <= circ_mv <= 50:
        f_score = 8
    elif 50 < circ_mv <= 200:
        f_score = 5
    elif 10 <= circ_mv < 20:
        f_score = 3
    elif circ_mv > 200:
        f_score = 2
    else:
        f_score = 0  # 太小，流动性差
    score += f_score

    # 因子G：技术形态（5分）- 基础分
    score += 3

    # 来源权重加成
    weight = float(row.get('source_weight', 0.5))
    final_score = score * weight + score * (1 - weight) * 0.8

    return round(final_score), {
        '涨停质量': a_score,
        '连板动量': b_score,
        '资金流向': c_score,
        '换手情绪': d_score,
        '题材板块': 8,
        '流动性': f_score,
        '技术形态': 3,
        '流通市值亿': round(circ_mv, 1),
        '换手率%': round(turnover, 2),
        '来源': source
    }

# ============================================================
# 一票否决过滤
# ============================================================
def is_rejected(row):
    """检查是否触发一票否决"""
    code = str(row.get('code', ''))
    name = str(row.get('name', ''))

    # ST股
    if 'ST' in name.upper() or 'ST' in code.upper():
        return "ST股"

    # 连板过多
    days = int(row.get('days', 1) or 1)
    if days > MAX_DAYS:
        return f"连板{days}板，高风险"

    # 市值过小
    circ_mv = float(row.get('circ_mv', 0) or 0)
    if circ_mv > 1e8:
        circ_mv = circ_mv / 1e8
    elif circ_mv > 1000:
        circ_mv = circ_mv / 10000
    if 0 < circ_mv < MIN_MARKET_CAP:
        return f"市值{circ_mv:.1f}亿过小"

    # 换手率过低
    turnover = float(row.get('turnover', 0) or 0)
    if 0 < turnover < MIN_TURNOVER:
        return f"换手率{turnover:.1f}%过低"

    return None

# ============================================================
# 主程序
# ============================================================
def main():
    print("=" * 55)
    print("🚀 A股双名单综合筛选系统 v2（AkShare免费版）")
    print("=" * 55)
    print(f"📅 今日日期: {get_today()}")

    # 1. 获取两个名单
    limit_up_df = fetch_limit_up_list()
    moneyflow_df = fetch_moneyflow_list()

    # 2. 合并两个名单
    all_stocks = []

    if not limit_up_df.empty and 'code' in limit_up_df.columns:
        for _, row in limit_up_df.iterrows():
            all_stocks.append({
                'code': str(row.get('code', '')),
                'name': str(row.get('name', '')),
                'pct_chg': row.get('pct_chg', 0),
                'days': row.get('days', 1),
                'turnover': row.get('turnover', 0),
                'circ_mv': row.get('circ_mv', 0),
                'limit_amount': row.get('limit_amount', 0),
                'net_flow': 0,
                'source': 'limit_up',
                'source_weight': 0.6
            })

    if not moneyflow_df.empty and 'code' in moneyflow_df.columns:
        existing_codes = {s['code'] for s in all_stocks}
        for _, row in moneyflow_df.iterrows():
            code = str(row.get('code', ''))
            if code in existing_codes:
                # 已在涨停名单，加分叠加
                for s in all_stocks:
                    if s['code'] == code:
                        s['net_flow'] = row.get('net_flow', 0)
                        s['source'] = 'both'  # 两个名单都有，最强信号
                        s['source_weight'] = 1.0  # 满权重
                        break
            else:
                all_stocks.append({
                    'code': code,
                    'name': str(row.get('name', '')),
                    'pct_chg': row.get('pct_chg', 0),
                    'days': row.get('days', 1),
                    'turnover': row.get('turnover', 0),
                    'circ_mv': row.get('circ_mv', 0),
                    'limit_amount': 0,
                    'net_flow': row.get('net_flow', 0),
                    'source': 'moneyflow',
                    'source_weight': 0.4
                })

    print(f"\n📊 两个名单合并后共 {len(all_stocks)} 只候选股票")
    print(f"   - 仅涨停名单: {sum(1 for s in all_stocks if s['source']=='limit_up')}只")
    print(f"   - 仅主力流入: {sum(1 for s in all_stocks if s['source']=='moneyflow')}只")
    print(f"   - 两个名单都有（最强信号）: {sum(1 for s in all_stocks if s['source']=='both')}只")

    # 3. 过滤 + 评分
    results = []
    rejected_count = 0

    for stock in all_stocks:
        reject_reason = is_rejected(stock)
        if reject_reason:
            rejected_count += 1
            continue

        score, details = score_stock(stock, stock['source'])
        results.append({
            'code': stock['code'],
            'name': stock['name'],
            'score': score,
            'details': details,
            'pct_chg': stock['pct_chg'],
            'days': stock['days'],
            'source': stock['source']
        })

    print(f"\n⛔ 一票否决淘汰 {rejected_count} 只")

    # 4. 按评分排序
    results.sort(key=lambda x: x['score'], reverse=True)
    top20 = results[:MAX_STOCKS]

    # 5. 输出结果
    print("\n" + "=" * 55)
    print(f"🏆 综合评分 Top{len(top20)}")
    print("=" * 55)

    source_icon = {
        'limit_up': '🔴涨停',
        'moneyflow': '💰流入',
        'both': '⭐双榜'
    }

    stock_codes = []
    for i, item in enumerate(top20, 1):
        code = item['code']
        # 转换为带后缀的格式
        if code.startswith('6'):
            ts_code = f"{code}.SH"
        elif code.startswith(('0', '3')):
            ts_code = f"{code}.SZ"
        elif code.startswith(('4', '8')):
            ts_code = f"{code}.BJ"
        else:
            ts_code = f"{code}.SH"

        stock_codes.append(ts_code)
        src = source_icon.get(item['source'], '📊')
        print(f"  {i:2d}. {code} {item['name'][:6]} | 评分:{item['score']:3d} | {src} | {item['days']}板 | {item['pct_chg']:.1f}%")

    # 6. 写入结果文件
    stock_list_str = ','.join(stock_codes)
    print(f"\n✅ STOCK_LIST={stock_list_str}")

    with open('screened_stocks.txt', 'w') as f:
        f.write(stock_list_str)

    print(f"\n📝 已写入 screened_stocks.txt")
    print("=" * 55)

if __name__ == '__main__':
    main()
