#!/usr/bin/env python3
"""
A股涨停自动筛选脚本
每日运行，从Tushare获取当日涨停股票名单
按七因子模型初步筛选，输出Top20候选
自动写入STOCK_LIST供daily_stock_analysis分析
"""

import os
import sys
import tushare as ts
import pandas as pd
from datetime import datetime, timedelta
import json

# ===== 配置 =====
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
MAX_STOCKS = 20          # 最多分析20只
MIN_MARKET_CAP = 10      # 最小流通市值（亿元），低于此值淘汰
MAX_MARKET_CAP = 500     # 最大流通市值（亿元），超过此值短线弹性差
MIN_TURNOVER = 2         # 最小换手率(%)，低于此值流动性差
MAX_TURNOVER = 25        # 最大换手率(%)，超过此值过热

def get_trade_date():
    """获取最近交易日"""
    today = datetime.now().strftime('%Y%m%d')
    return today

def fetch_limit_up_stocks(pro, trade_date):
    """从Tushare获取当日涨停股票"""
    print(f"📡 正在获取 {trade_date} 涨停名单...")
    try:
        # 获取涨停股票（涨幅>=9.9%视为涨停）
        df = pro.limit_list_d(
            trade_date=trade_date,
            limit_type='U'  # U=涨停
        )
        if df is None or df.empty:
            print("⚠️ 今日涨停数据为空，尝试获取行情数据...")
            return fetch_limit_up_from_daily(pro, trade_date)
        print(f"✅ 获取到涨停股 {len(df)} 只")
        return df
    except Exception as e:
        print(f"⚠️ 涨停接口失败: {e}，改用行情数据...")
        return fetch_limit_up_from_daily(pro, trade_date)

def fetch_limit_up_from_daily(pro, trade_date):
    """从日线数据筛选涨停股（备用方案）"""
    try:
        df = pro.daily(trade_date=trade_date)
        if df is None or df.empty:
            return pd.DataFrame()
        # 筛选涨幅>=9.9%的股票
        limit_up = df[df['pct_chg'] >= 9.9].copy()
        limit_up.rename(columns={'ts_code': 'ts_code', 'pct_chg': 'pct_chg'}, inplace=True)
        print(f"✅ 从日线数据筛选到涨停股 {len(limit_up)} 只")
        return limit_up
    except Exception as e:
        print(f"❌ 日线数据也失败: {e}")
        return pd.DataFrame()

def fetch_stock_basics(pro, stock_list):
    """获取股票基本信息（市值、换手率等）"""
    print(f"📊 正在获取 {len(stock_list)} 只股票的基本数据...")
    try:
        codes = ','.join(stock_list)
        df = pro.daily_basic(
            ts_code=codes,
            trade_date=get_trade_date(),
            fields='ts_code,turnover_rate,volume_ratio,pe,pb,circ_mv,total_mv'
        )
        return df
    except Exception as e:
        print(f"⚠️ 基本数据获取失败: {e}")
        return pd.DataFrame()

def fetch_moneyflow(pro, stock_list, trade_date):
    """获取资金流向数据"""
    print(f"💰 正在获取资金流向数据...")
    try:
        codes = ','.join(stock_list)
        df = pro.moneyflow(
            ts_code=codes,
            trade_date=trade_date,
            fields='ts_code,buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount'
        )
        return df
    except Exception as e:
        print(f"⚠️ 资金流向获取失败: {e}")
        return pd.DataFrame()

def filter_st_stocks(stock_list):
    """过滤ST股票"""
    filtered = [s for s in stock_list if not any(
        x in s for x in ['ST', 'st']
    )]
    return filtered

def score_stocks(limit_up_df, basics_df, moneyflow_df):
    """
    七因子评分模型（简化版，基于可获取的Tushare数据）
    满分100分
    """
    scores = []

    for _, row in limit_up_df.iterrows():
        ts_code = row.get('ts_code', '')
        if not ts_code:
            continue

        score = 0
        details = {}

        # 获取基本面数据
        basic = basics_df[basics_df['ts_code'] == ts_code] if not basics_df.empty else pd.DataFrame()
        mf = moneyflow_df[moneyflow_df['ts_code'] == ts_code] if not moneyflow_df.empty else pd.DataFrame()

        # ===== 因子A：涨停质量（22分）=====
        # 基于连板天数和涨幅
        fd_days = row.get('fd_amount', 0)  # 封单金额
        pct_chg = row.get('pct_chg', 0)

        if pct_chg >= 19.5:  # 20CM涨停（科创/创业板）
            score += 20
            details['涨停质量'] = 20
        elif pct_chg >= 9.9:  # 正常涨停
            score += 15
            details['涨停质量'] = 15
        else:
            details['涨停质量'] = 0

        # ===== 因子B：连板动量（18分）=====
        # Tushare limit_list_d有连板数据
        days_up = row.get('days', 1)
        if isinstance(days_up, (int, float)):
            if days_up >= 4:
                b_score = 18
            elif days_up == 3:
                b_score = 14
            elif days_up == 2:
                b_score = 10
            else:
                b_score = 6
        else:
            b_score = 6
        score += b_score
        details['连板动量'] = b_score

        # ===== 因子C：资金流向（20分）=====
        if not mf.empty:
            net_mf = mf.iloc[0].get('net_mf_amount', 0) or 0
            buy_elg = mf.iloc[0].get('buy_elg_amount', 0) or 0
            sell_elg = mf.iloc[0].get('sell_elg_amount', 0) or 0
            net_elg = buy_elg - sell_elg

            if net_mf > 5000:  # 净流入>5000万
                c_score = 18
            elif net_mf > 2000:
                c_score = 14
            elif net_mf > 0:
                c_score = 10
            else:
                c_score = 3
            score += c_score
            details['资金流向'] = c_score
        else:
            score += 8  # 无数据给默认分
            details['资金流向'] = 8

        # ===== 因子D：情绪竞价（15分）- 用量比替代 =====
        if not basic.empty:
            vol_ratio = basic.iloc[0].get('volume_ratio', 1) or 1
            if vol_ratio >= 3:
                d_score = 15
            elif vol_ratio >= 2:
                d_score = 12
            elif vol_ratio >= 1.5:
                d_score = 8
            else:
                d_score = 4
            score += d_score
            details['量比/情绪'] = d_score
        else:
            score += 7
            details['量比/情绪'] = 7

        # ===== 因子E：题材板块（12分）- 暂给基础分 =====
        # Tushare无法实时获取题材评分，给基础分
        score += 8
        details['题材板块'] = 8

        # ===== 因子F：流动性（8分）=====
        if not basic.empty:
            circ_mv = (basic.iloc[0].get('circ_mv', 0) or 0) / 10000  # 转换为亿元
            turnover = basic.iloc[0].get('turnover_rate', 0) or 0

            # 市值评分
            if 20 <= circ_mv <= 50:
                mv_score = 4
            elif 50 < circ_mv <= 200:
                mv_score = 3
            elif circ_mv < 20:
                mv_score = 1
            else:
                mv_score = 1

            # 换手率评分
            if 5 <= turnover <= 15:
                to_score = 4
            elif 2 <= turnover < 5 or 15 < turnover <= 25:
                to_score = 2
            else:
                to_score = 0

            f_score = mv_score + to_score
            score += f_score
            details['流动性'] = f_score
            details['流通市值亿'] = round(circ_mv, 1)
            details['换手率%'] = round(turnover, 2)
        else:
            score += 4
            details['流动性'] = 4

        # ===== 因子G：技术形态（5分）=====
        # 基于量比和涨幅简单判断
        score += 3  # 基础分
        details['技术形态'] = 3

        scores.append({
            'ts_code': ts_code,
            'score': round(score),
            'details': details,
            'pct_chg': pct_chg,
            'days': row.get('days', 1)
        })

    # 按评分排序
    scores.sort(key=lambda x: x['score'], reverse=True)
    return scores

def apply_filters(scores, basics_df):
    """应用一票否决过滤规则"""
    filtered = []
    rejected = []

    for item in scores:
        ts_code = item['ts_code']
        reason = None

        # 过滤ST股
        if 'ST' in ts_code or 'st' in ts_code:
            reason = "ST股"

        # 过滤市值太小或太大
        if not basics_df.empty:
            basic = basics_df[basics_df['ts_code'] == ts_code]
            if not basic.empty:
                circ_mv = (basic.iloc[0].get('circ_mv', 0) or 0) / 10000
                turnover = basic.iloc[0].get('turnover_rate', 0) or 0

                if circ_mv < MIN_MARKET_CAP:
                    reason = f"市值过小({circ_mv:.1f}亿)"
                elif turnover < MIN_TURNOVER:
                    reason = f"换手率过低({turnover:.1f}%)"

        # 过滤连板过多（高风险区域）
        if item.get('days', 1) > 7:
            reason = f"连板过多({item['days']}板，高风险)"

        if reason:
            rejected.append({'ts_code': ts_code, 'reason': reason})
        else:
            filtered.append(item)

    if rejected:
        print(f"\n⛔ 一票否决（共{len(rejected)}只）:")
        for r in rejected[:5]:  # 只显示前5个
            print(f"   {r['ts_code']}: {r['reason']}")

    return filtered

def main():
    if not TUSHARE_TOKEN:
        print("❌ 错误：未设置TUSHARE_TOKEN环境变量")
        sys.exit(1)

    print("=" * 50)
    print("🚀 A股涨停自动筛选系统启动")
    print("=" * 50)

    # 初始化Tushare
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()

    trade_date = get_trade_date()
    print(f"📅 交易日期: {trade_date}")

    # 1. 获取涨停名单
    limit_up_df = fetch_limit_up_stocks(pro, trade_date)

    if limit_up_df.empty:
        print("❌ 未获取到涨停数据，退出")
        # 输出空结果
        print("\nSTOCK_LIST=")
        sys.exit(0)

    print(f"\n📈 今日涨停股票共 {len(limit_up_df)} 只")

    # 2. 获取股票代码列表
    stock_list = limit_up_df['ts_code'].tolist() if 'ts_code' in limit_up_df.columns else []

    # 过滤ST股
    stock_list = [s for s in stock_list if 'ST' not in s.upper()]
    print(f"✅ 过滤ST后剩余 {len(stock_list)} 只")

    # 3. 获取基本数据
    basics_df = fetch_stock_basics(pro, stock_list[:50])  # 最多取前50只

    # 4. 获取资金流向
    moneyflow_df = fetch_moneyflow(pro, stock_list[:50], trade_date)

    # 5. 七因子评分
    print("\n🔢 正在进行七因子评分...")
    scores = score_stocks(limit_up_df, basics_df, moneyflow_df)

    # 6. 应用过滤规则
    filtered = apply_filters(scores, basics_df)

    # 7. 取Top20
    top20 = filtered[:MAX_STOCKS]

    # 8. 输出结果
    print("\n" + "=" * 50)
    print(f"🏆 七因子评分 Top{len(top20)} 候选股票")
    print("=" * 50)

    stock_codes = []
    for i, item in enumerate(top20, 1):
        code = item['ts_code']
        score = item['score']
        days = item.get('days', 1)
        pct = item.get('pct_chg', 0)
        stock_codes.append(code)
        print(f"  {i:2d}. {code} | 评分:{score:3d} | {days}连板 | 涨幅:{pct:.1f}%")

    # 9. 输出STOCK_LIST供workflow使用
    stock_list_str = ','.join(stock_codes)
    print(f"\n✅ STOCK_LIST={stock_list_str}")

    # 写入文件供workflow读取
    with open('screened_stocks.txt', 'w') as f:
        f.write(stock_list_str)

    print(f"\n📝 已写入 screened_stocks.txt")
    print("=" * 50)

if __name__ == '__main__':
    main()
