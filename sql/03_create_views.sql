-- ============================================================
-- 03_create_views.sql
-- 便捷视图：日常查询、数据质量核对、因子研究取数
-- ============================================================

USE `quant`;

-- 前复权行情（最常用，因子与信号默认用它）
CREATE OR REPLACE VIEW `v_price_qfq` AS
SELECT `trade_date`, `ts_code`, `open`, `high`, `low`, `close`, `pre_close`,
       `change_pct`, `volume`, `amount`, `turnover_rate`,
       `status`, `is_limit_up`, `is_limit_down`, `is_suspended`
FROM `daily_price`
WHERE `adj_type` = 'qfq';

-- 后复权行情（收益计算用）
CREATE OR REPLACE VIEW `v_price_hfq` AS
SELECT `trade_date`, `ts_code`, `open`, `high`, `low`, `close`,
       `change_pct`, `volume`, `amount`, `turnover_rate`, `status`
FROM `daily_price`
WHERE `adj_type` = 'hfq';

-- 行情 + 市值估值 联表（因子层主力视图，避免每次 JOIN）
CREATE OR REPLACE VIEW `v_panel` AS
SELECT  p.`trade_date`,
        p.`ts_code`,
        p.`open`, p.`high`, p.`low`, p.`close`, p.`pre_close`,
        p.`change_pct`, p.`volume`, p.`amount`, p.`turnover_rate`,
        p.`status`, p.`is_limit_up`, p.`is_limit_down`, p.`is_suspended`,
        b.`total_mv`, b.`float_mv`, b.`total_share`, b.`float_share`,
        b.`pe_ttm`, b.`pb`,
        s.`name`, s.`board`, s.`list_date`, s.`delist_date`, s.`is_delisted`
FROM `daily_price` p
LEFT JOIN `daily_basic` b
       ON p.`trade_date` = b.`trade_date` AND p.`ts_code` = b.`ts_code`
LEFT JOIN `stock_basic` s
       ON p.`ts_code` = s.`ts_code`
WHERE p.`adj_type` = 'qfq';

-- 每个交易日的股票数（快速看数据完整度）
CREATE OR REPLACE VIEW `v_coverage_by_date` AS
SELECT `trade_date`, COUNT(DISTINCT `ts_code`) AS `stock_count`
FROM `daily_price`
WHERE `adj_type` = 'qfq'
GROUP BY `trade_date`;

-- 每只股票的覆盖区间
CREATE OR REPLACE VIEW `v_coverage_by_code` AS
SELECT `ts_code`,
       MIN(`trade_date`) AS `first_date`,
       MAX(`trade_date`) AS `last_date`,
       COUNT(*)          AS `rows`
FROM `daily_price`
WHERE `adj_type` = 'qfq'
GROUP BY `ts_code`;

-- 数据质量体检：异常行一眼看全
CREATE OR REPLACE VIEW `v_data_quality` AS
SELECT `trade_date`, `ts_code`, `adj_type`,
       CASE
         WHEN `close` IS NULL OR `close` <= 0                       THEN 'close_nonpositive'
         WHEN `high` < `low`                                        THEN 'high_lt_low'
         WHEN `close` > `high` OR `close` < `low`                   THEN 'close_out_of_range'
         WHEN `open`  > `high` OR `open`  < `low`                   THEN 'open_out_of_range'
         WHEN `volume` < 0 OR `amount` < 0                          THEN 'negative_volume'
         WHEN ABS(`change_pct`) > 30                                THEN 'abnormal_change'
         ELSE NULL
       END AS `issue`
FROM `daily_price`
HAVING `issue` IS NOT NULL;
