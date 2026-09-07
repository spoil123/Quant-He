-- ============================================================
-- 02_create_tables.sql
-- 本地金融数据库表结构（第 1 层：数据层）
--
-- 设计原则：
--  1) 日线表按年分区。5000+ 只股票 × 10 年 ≈ 1200 万行/复权类型，
--     不分区的话任何按日期范围扫描都会退化成全表扫。
--  2) 主键直接用 (trade_date, ts_code, adj_type) 而非自增 id：
--     - MySQL 要求分区列必须出现在所有唯一键中，自增 id 做不到；
--     - 业务天然唯一，可直接 INSERT IGNORE 做幂等增量写入。
--  3) 金额统一为「元」，万股/万元在入库前由清洗层换算，避免口径混乱。
--  4) 财务表一律同时存 report_date（报告期）与 ann_date（数据可用日），
--     ann_date 是杜绝前视偏差的唯一锚点。
-- ============================================================

USE `quant`;

SET NAMES utf8mb4;

-- ----------------------------------------------------------------
-- 1. 股票基本信息（含退市 —— 消除幸存者偏差的关键）
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `stock_basic` (
  `ts_code`      VARCHAR(10)  NOT NULL                COMMENT '6位代码',
  `name`         VARCHAR(64)           DEFAULT NULL   COMMENT '股票名称',
  `exchange`     ENUM('SH','SZ','BJ')  NOT NULL       COMMENT '交易所',
  `board`        VARCHAR(16)           DEFAULT NULL   COMMENT '主板/创业板/科创板/北交所',
  `list_date`    DATE                  DEFAULT NULL   COMMENT '上市日期',
  `delist_date`  DATE                  DEFAULT NULL   COMMENT '退市日期，NULL=在市',
  `is_delisted`  TINYINT      NOT NULL DEFAULT 0      COMMENT '1=已退市',
  `is_st`        TINYINT      NOT NULL DEFAULT 0      COMMENT '1=当前ST/*ST（时点状态见 daily_price.status）',
  `status`       TINYINT      NOT NULL DEFAULT 1      COMMENT '0=退市 1=正常上市 2=暂停上市 3=退市整理期',
  `updated_at`   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`ts_code`),
  KEY `idx_delist` (`is_delisted`, `delist_date`),
  KEY `idx_board` (`board`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股票主表（含退市股）';


-- ----------------------------------------------------------------
-- 2. 日线行情（前复权 / 后复权 双份）
--    adj_type: qfq=前复权（因子计算用） hfq=后复权（真实收益用） none=不复权
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `daily_price` (
  `trade_date`    DATE             NOT NULL                COMMENT '交易日',
  `ts_code`       VARCHAR(10)      NOT NULL                COMMENT '6位代码',
  `adj_type`      ENUM('qfq','hfq','none') NOT NULL        COMMENT '复权类型',
  `open`          DECIMAL(12,4)             DEFAULT NULL,
  `high`          DECIMAL(12,4)             DEFAULT NULL,
  `low`           DECIMAL(12,4)             DEFAULT NULL,
  `close`         DECIMAL(12,4)             DEFAULT NULL,
  `pre_close`     DECIMAL(12,4)             DEFAULT NULL   COMMENT '前收盘（复权口径）',
  `change`        DECIMAL(12,4)             DEFAULT NULL   COMMENT '涨跌额',
  `change_pct`    DECIMAL(10,4)             DEFAULT NULL   COMMENT '涨跌幅 %',
  `volume`        DECIMAL(22,2)             DEFAULT NULL   COMMENT '成交量（股）',
  `amount`        DECIMAL(22,4)             DEFAULT NULL   COMMENT '成交额（元）',
  `turnover_rate` DECIMAL(12,6)             DEFAULT NULL   COMMENT '换手率 %',
  `amplitude`     DECIMAL(10,4)             DEFAULT NULL   COMMENT '振幅 %',
  `status`        TINYINT          NOT NULL DEFAULT 1      COMMENT '0=停牌 1=正常 2=ST 3=退市整理期',
  `is_limit_up`   TINYINT          NOT NULL DEFAULT 0      COMMENT '1=涨停（收盘价封板）',
  `is_limit_down` TINYINT          NOT NULL DEFAULT 0      COMMENT '1=跌停',
  `is_suspended`  TINYINT          NOT NULL DEFAULT 0      COMMENT '1=当日停牌（成交量为0）',
  `updated_at`    TIMESTAMP        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`trade_date`, `ts_code`, `adj_type`),
  KEY `idx_code_date` (`ts_code`, `trade_date`),
  KEY `idx_adj` (`adj_type`, `trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='日线行情（分区表）'
PARTITION BY RANGE COLUMNS(`trade_date`) (
  PARTITION p2015 VALUES LESS THAN ('2016-01-01'),
  PARTITION p2016 VALUES LESS THAN ('2017-01-01'),
  PARTITION p2017 VALUES LESS THAN ('2018-01-01'),
  PARTITION p2018 VALUES LESS THAN ('2019-01-01'),
  PARTITION p2019 VALUES LESS THAN ('2020-01-01'),
  PARTITION p2020 VALUES LESS THAN ('2021-01-01'),
  PARTITION p2021 VALUES LESS THAN ('2022-01-01'),
  PARTITION p2022 VALUES LESS THAN ('2023-01-01'),
  PARTITION p2023 VALUES LESS THAN ('2024-01-01'),
  PARTITION p2024 VALUES LESS THAN ('2025-01-01'),
  PARTITION p2025 VALUES LESS THAN ('2026-01-01'),
  PARTITION p2026 VALUES LESS THAN ('2027-01-01'),
  PARTITION p2027 VALUES LESS THAN ('2028-01-01'),
  PARTITION p2028 VALUES LESS THAN ('2029-01-01'),
  PARTITION p2029 VALUES LESS THAN ('2030-01-01'),
  PARTITION p2030 VALUES LESS THAN ('2031-01-01'),
  PARTITION pmax   VALUES LESS THAN (MAXVALUE)
);


-- ----------------------------------------------------------------
-- 3. 复权因子（用于自行校验与不复权价还原）
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `adj_factor` (
  `trade_date`  DATE          NOT NULL,
  `ts_code`     VARCHAR(10)   NOT NULL,
  `adj_factor`  DECIMAL(18,8) DEFAULT NULL COMMENT '后复权因子',
  `updated_at`  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`ts_code`, `trade_date`),
  KEY `idx_date` (`trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='复权因子';


-- ----------------------------------------------------------------
-- 4. 每日指标（市值、估值 —— 因子层与加权的核心输入）
--    total_mv / float_mv 单位：元（入库前已由万元换算）
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `daily_basic` (
  `trade_date`   DATE         NOT NULL,
  `ts_code`      VARCHAR(10)  NOT NULL,
  `close`        DECIMAL(12,4)          DEFAULT NULL COMMENT '当日收盘（不复权）',
  `total_mv`     DECIMAL(22,4)          DEFAULT NULL COMMENT '总市值（元）',
  `float_mv`     DECIMAL(22,4)          DEFAULT NULL COMMENT '流通市值（元）',
  `total_share`  DECIMAL(22,2)          DEFAULT NULL COMMENT '总股本（股）',
  `float_share`  DECIMAL(22,2)          DEFAULT NULL COMMENT '流通股本（股）',
  `pe_ttm`       DECIMAL(16,4)          DEFAULT NULL,
  `pb`           DECIMAL(16,4)          DEFAULT NULL,
  `ps_ttm`       DECIMAL(16,4)          DEFAULT NULL,
  `dv_ttm`       DECIMAL(12,6)          DEFAULT NULL COMMENT '股息率 %',
  `turnover_rate` DECIMAL(12,6)         DEFAULT NULL COMMENT '换手率 %',
  `updated_at`   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`trade_date`, `ts_code`),
  KEY `idx_code_date` (`ts_code`, `trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日市值与估值指标'
PARTITION BY RANGE COLUMNS(`trade_date`) (
  PARTITION p2015 VALUES LESS THAN ('2016-01-01'),
  PARTITION p2016 VALUES LESS THAN ('2017-01-01'),
  PARTITION p2017 VALUES LESS THAN ('2018-01-01'),
  PARTITION p2018 VALUES LESS THAN ('2019-01-01'),
  PARTITION p2019 VALUES LESS THAN ('2020-01-01'),
  PARTITION p2020 VALUES LESS THAN ('2021-01-01'),
  PARTITION p2021 VALUES LESS THAN ('2022-01-01'),
  PARTITION p2022 VALUES LESS THAN ('2023-01-01'),
  PARTITION p2023 VALUES LESS THAN ('2024-01-01'),
  PARTITION p2024 VALUES LESS THAN ('2025-01-01'),
  PARTITION p2025 VALUES LESS THAN ('2026-01-01'),
  PARTITION p2026 VALUES LESS THAN ('2027-01-01'),
  PARTITION p2027 VALUES LESS THAN ('2028-01-01'),
  PARTITION p2028 VALUES LESS THAN ('2029-01-01'),
  PARTITION p2029 VALUES LESS THAN ('2030-01-01'),
  PARTITION p2030 VALUES LESS THAN ('2031-01-01'),
  PARTITION pmax   VALUES LESS THAN (MAXVALUE)
);


-- ----------------------------------------------------------------
-- 5. 财务指标（按报告期）
--    ann_date 是唯一可用时点锚点：因子在 T 日只能用 ann_date <= T 的行
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `financial_indicator` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `ts_code`           VARCHAR(10)  NOT NULL,
  `report_date`       DATE         NOT NULL                COMMENT '报告期，如 2023-12-31',
  `report_type`       VARCHAR(12)           DEFAULT NULL   COMMENT 'annual/semi/quarter1/quarter3',
  `ann_date`          DATE                  DEFAULT NULL   COMMENT '数据可用日（真实公告日或法定最迟披露日）',
  `ann_date_source`   TINYINT      NOT NULL DEFAULT 0      COMMENT '0=法定截止日推算 1=真实公告日',
  `eps`               DECIMAL(14,4)         DEFAULT NULL,
  `bps`               DECIMAL(14,4)         DEFAULT NULL,
  `roe`               DECIMAL(12,6)         DEFAULT NULL   COMMENT '净资产收益率（单期，%）',
  `roe_ttm`           DECIMAL(12,6)         DEFAULT NULL   COMMENT 'ROE TTM（%）',
  `net_profit`        DECIMAL(22,4)         DEFAULT NULL   COMMENT '净利润（元）',
  `net_profit_ttm`    DECIMAL(22,4)         DEFAULT NULL   COMMENT '净利润 TTM（元）',
  `total_revenue`     DECIMAL(22,4)         DEFAULT NULL   COMMENT '营业收入（元）',
  `total_revenue_ttm` DECIMAL(22,4)         DEFAULT NULL,
  `total_assets`      DECIMAL(22,4)         DEFAULT NULL,
  `total_equity`      DECIMAL(22,4)         DEFAULT NULL   COMMENT '所有者权益（元）',
  `gross_margin`      DECIMAL(12,6)         DEFAULT NULL   COMMENT '毛利率 %',
  `debt_ratio`        DECIMAL(12,6)         DEFAULT NULL   COMMENT '资产负债率 %',
  `updated_at`        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_code_report` (`ts_code`, `report_date`),
  KEY `idx_ann_date` (`ann_date`),
  KEY `idx_code_ann` (`ts_code`, `ann_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='财务指标（含数据可用日）';


-- ----------------------------------------------------------------
-- 6. 行业分类（申万一级，截面中性化用）
--    effective_date 起支持行业变更历史，避免用未来行业解释过去
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `industry_classification` (
  `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `ts_code`         VARCHAR(10) NOT NULL,
  `industry_code`   VARCHAR(16)          DEFAULT NULL,
  `industry_name`   VARCHAR(64)          DEFAULT NULL,
  `source`          VARCHAR(16) NOT NULL DEFAULT 'sw_l1' COMMENT 'sw_l1=申万一级',
  `effective_date`  DATE                 DEFAULT NULL   COMMENT '生效日，NULL=全期有效',
  `updated_at`      TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_code_source_date` (`ts_code`, `source`, `effective_date`),
  KEY `idx_industry` (`industry_name`),
  KEY `idx_source` (`source`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='行业分类';


-- ----------------------------------------------------------------
-- 7. 指数日线（基准：沪深300）
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `index_daily` (
  `trade_date`  DATE         NOT NULL,
  `index_code`  VARCHAR(10)  NOT NULL,
  `open`        DECIMAL(14,4)         DEFAULT NULL,
  `high`        DECIMAL(14,4)         DEFAULT NULL,
  `low`         DECIMAL(14,4)         DEFAULT NULL,
  `close`       DECIMAL(14,4)         DEFAULT NULL,
  `change_pct`  DECIMAL(10,4)         DEFAULT NULL COMMENT '涨跌幅 %',
  `volume`      DECIMAL(22,2)         DEFAULT NULL,
  `amount`      DECIMAL(22,4)         DEFAULT NULL,
  `updated_at`  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`trade_date`, `index_code`),
  KEY `idx_code_date` (`index_code`, `trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='指数日线（基准）';


-- ----------------------------------------------------------------
-- 8. 交易日历
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `trade_calendar` (
  `trade_date`   DATE      NOT NULL,
  `is_trading`   TINYINT   NOT NULL DEFAULT 1,
  `prev_date`    DATE               DEFAULT NULL COMMENT '上一交易日',
  `next_date`    DATE               DEFAULT NULL COMMENT '下一交易日',
  `is_month_end` TINYINT   NOT NULL DEFAULT 0   COMMENT '1=当月最后一个交易日（调仓用）',
  PRIMARY KEY (`trade_date`),
  KEY `idx_month_end` (`is_month_end`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='交易日历';


-- ----------------------------------------------------------------
-- 9. 数据更新日志（增量更新的断点与审计）
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `update_log` (
  `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `task_name`    VARCHAR(64)  NOT NULL                COMMENT '任务名，如 daily_price/stock_basic',
  `ts_code`      VARCHAR(10)           DEFAULT NULL   COMMENT '单票任务时记录，全量任务为 NULL',
  `start_date`   DATE                  DEFAULT NULL,
  `end_date`     DATE                  DEFAULT NULL,
  `rows_written` INT          NOT NULL DEFAULT 0,
  `status`       ENUM('success','failed','skipped') NOT NULL DEFAULT 'success',
  `message`      VARCHAR(512)          DEFAULT NULL,
  `started_at`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `finished_at`  DATETIME              DEFAULT NULL,
  `duration_sec` DECIMAL(10,2)         DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_task` (`task_name`, `started_at`),
  KEY `idx_code` (`ts_code`, `task_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据更新日志';


-- ----------------------------------------------------------------
-- 10. 因子暴露（第 3 层产出，落库便于复盘与加速）
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `factor_exposure` (
  `trade_date`  DATE         NOT NULL,
  `ts_code`     VARCHAR(10)  NOT NULL,
  `factor_name` VARCHAR(32)  NOT NULL,
  `raw_value`   DOUBLE                DEFAULT NULL COMMENT '原始值（去极值前）',
  `value`       DOUBLE                DEFAULT NULL COMMENT '预处理后标准分',
  `updated_at`  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`trade_date`, `ts_code`, `factor_name`),
  KEY `idx_factor` (`factor_name`, `trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='因子暴露面板';


-- ----------------------------------------------------------------
-- 11. 回测结果（第 2 层产出，用于参数敏感性对比）
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `backtest_result` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `run_name`          VARCHAR(64)  NOT NULL,
  `strategy`          VARCHAR(64)           DEFAULT NULL,
  `start_date`        DATE                  DEFAULT NULL,
  `end_date`          DATE                  DEFAULT NULL,
  `initial_cash`      DECIMAL(20,2)         DEFAULT NULL,
  `final_value`       DECIMAL(20,2)         DEFAULT NULL,
  `total_return`      DECIMAL(12,6)         DEFAULT NULL,
  `annual_return`     DECIMAL(12,6)         DEFAULT NULL,
  `sharpe`            DECIMAL(12,6)         DEFAULT NULL,
  `max_drawdown`      DECIMAL(12,6)         DEFAULT NULL,
  `benchmark_return`  DECIMAL(12,6)         DEFAULT NULL,
  `excess_return`     DECIMAL(12,6)         DEFAULT NULL,
  `turnover`          DECIMAL(12,6)         DEFAULT NULL,
  `params_json`       JSON                  DEFAULT NULL COMMENT '本次运行的完整参数快照',
  `created_at`        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_run` (`run_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='回测结果归档';
